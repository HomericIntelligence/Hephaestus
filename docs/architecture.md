# Hephaestus Architecture Reference

This document is the canonical, source-grounded architecture reference for
the Hephaestus automation pipeline and supporting subsystems. Every
operational claim links to the module that backs it.
The [`docs/adr/`](adr/) records remain the bind-points for individual
architectural decisions (`0006-queue-based-in-process-automation-pipeline`,
…) — this document is the unified reference; ADRs are the historical record.
This file is source-grounded: every operational claim links to the module
that backs it, in the form `[module/file.py](path/to/file.py)` or
`[§module/Class.func](path/to/file.py)`. Per the project convention
(`"Code References": 'DO'` in [`AGENTS.md`](../AGENTS.md) §"Claude Code
Optimization"), file paths are repo-relative.

---

## Table of contents

1. [Goals, non-goals and design principles](#1-goals-non-goals-and-design-principles)
2. [System overview: single coordinator, seven queues, one worker pool](#2-system-overview)
3. [Cross-cutting invariants](#3-cross-cutting-invariants)
4. [WorkItem and the durable journal](#4-workitem-and-the-durable-journal)
5. [The seven queue stages](#5-the-seven-queue-stages)

- [5.1 Repo intake](#51-repo-intake)
- [5.2 Planning](#52-planning)
- [5.3 Plan review](#53-plan-review)
- [5.4 Implementation](#54-implementation)
- [5.5 PR review](#55-pr-review)
- [5.6 Merge wait](#56-merge-wait)
- [5.7 Finished](#57-finished)

1. [The ROUTES table — single source of truth](#6-the-routes-table)
2. [Seeding and restart reconstruction](#7-seeding-and-restart-reconstruction)
3. [The worker pool and job contract](#8-the-worker-pool-and-job-contract)
4. [Thin CLI scope wrappers and rollout controls](#9-thin-cli-scope-wrappers)
5. [Observability, dry-run and rate-budget gate](#10-observability-dry-run-and-rate-budget-gate)
6. [Interrupt semantics and exit codes](#12-interrupt-semantics-and-exit-codes)
7. [Glossary](#13-glossary)

---

## 1. Goals, non-goals and design principles

### Goals

- **Single durable journal.** GitHub labels, comments, PR state and
 `ArmingStateStore` records are the only
 crash-resistant truth. Stages may not persist any other state. Restart =
re-run: queue reconstruction reads the journal
([`coordinator._seed_pass`](hephaestus/automation/pipeline/coordinator.py),
[`seed_from_cli`](hephaestus/automation/pipeline/seeding.py)) — distinct from
the per-repo seed-side [`repo._seed_pass`](hephaestus/automation/pipeline/stages/repo.py)
in §5.1, which tags `state:skip` on epics before any other durable mutation.
- **Interrupt = resumable, never failed.** A SIGINT/SIGTERM/SIGHUP during a
 run parks the touched item with `ItemResult(passed=False,
 reason="resumable at <stage>", …)`. A subsequent restart seeds it back
 into the same queue and the loop reconverges
 ([`_park_resumable`](hephaestus/automation/pipeline/coordinator.py),
 [`_finalize_resumable`](hephaestus/automation/pipeline/coordinator.py)).
- **Reviewed-head proof, conditional queue merge.** `state:implementation-go`
 is applied only by `pr_review._eval`, which is the sole stage authorized to
 write the label. Before the reviewer runs, `pr_review` snapshots the GitHub
 review inputs and its head SHA, then requires a clean local checkout at that
 exact SHA. The resulting in-memory proof is rechecked against a confirmed,
 unarmed live PR before the label is written. `merge_wait` uses that same
 active-run proof before every request in a bounded sequence (default: five)
 of SHA-conditional ordinary REST squash merges. Before a request, it may make
 a bounded read-only readiness wait (15 minutes per reviewed head) without
 spending a merge attempt; readiness is not authorization, and each request
 still has fresh open/`main`/unarmed/exclusive-GO admission.
 The direct adapter makes one request per call and never retries. No queue
 stage invokes `gh pr merge`, creates, disables, adopts, or polls native
 auto-merge, manages a merge queue, or uses an administrator bypass
 ([`pr_review.py`](hephaestus/automation/pipeline/stages/pr_review.py),
 [`worker_pool.py`](hephaestus/automation/pipeline/worker_pool.py),
 [`merge_wait.py`](hephaestus/automation/pipeline/stages/merge_wait.py)).
- **Globally bounded budgets.** Stages count retries on `_on_job_done` so
 `agent_error` retries consume the same per-item budget as ordinary
 attempts; cross-stage regression cycles terminate in finite steps
 ([`_FAIL_BACK_CAP`](hephaestus/automation/pipeline/coordinator.py),
 [`ROUTES`](hephaestus/automation/pipeline/routing.py)).
- **Globally bounded live work.** The coordinator admits at most
 `C = max(1, parallel_repos × max_workers)` nonterminal work items at once.
 A work permit remains with an item while it is queued, leased, running,
 timer-parked, or waiting for a full destination queue; it is released only
 after the finished sink records the terminal result
 ([`_work_window`](hephaestus/automation/pipeline/coordinator.py),
 [`Coordinator._push_item`](hephaestus/automation/pipeline/coordinator.py),
 [`Coordinator._release_work_permit`](hephaestus/automation/pipeline/coordinator.py)).

### Non-goals

- **No persisted queue snapshot.** Queues are in-memory; reconstruction
 reads GitHub via [`seeding.py`](hephaestus/automation/pipeline/seeding.py)
 ([`_all_idle`](hephaestus/automation/pipeline/coordinator.py) +
 [`_reseed_if_converged`](hephaestus/automation/pipeline/coordinator.py)).
- **No OS-level agent sandbox.** Each agent call site declares its explicit
 `--allowedTools` scope and runs in a scoped worktree
 ([`_run_agent`](hephaestus/automation/pipeline/worker_pool.py),
 [`agent_config.py`](hephaestus/automation/agent_config.py)).
- **No MCP runtime dependency.** `.mcp.json` is intentionally empty. Plugin
 marketplaces, NATS JetStream and HTTP REST remain the maintained
 integration contracts ([ADR-0011](adr/0011-mcp-integration-posture.md)).

### Design principles

- **KISS / YAGNI.** Each stage owns one responsibility. The deferred
 `AgentProtocol` and `resilience` wiring into the GitHub call path
 (issues #468, #469) are intentionally NOT built yet.
- **DRY / one-way dependency.** `automation → library` only — library
 subpackages may not import from
 [`hephaestus.automation`](hephaestus/automation/), as defined by
 [`ADR-0001`](adr/0001-automation-library-boundary.md).
- **SOLID / substitutable providers.** [`hephaestus.agents.runtime`](hephaestus/agents/runtime.py)
 abstracts over Claude Code and Codex behind a uniform `--agent` flag.
- **POLA. Least privilege, least astonishment.** Per-call
 `--allowedTools`, scoped worktrees, fenced untrusted GitHub content
 via `_fence_untrusted` in
 [`prompts/_shared.py`](hephaestus/automation/prompts/_shared.py) and
 admin-free human-gated merge.

---

## 2. System overview

### Topology

The default path is **`hephaestus-automation-loop`**, the queue-based
in-process pipeline whose coordinator lives at
[`hephaestus.automation.pipeline.coordinator`](hephaestus/automation/pipeline/coordinator.py).
The coordinator owns **seven in-memory stage queues** and dispatches
agent / build-test / git-network jobs to a single `WorkerPool`. Each agent
job runs Claude or Codex, chosen by `--agent` (default Claude).

#### Seven-stage queue block diagram

```mermaid
flowchart LR
    repo["1. Repo"] --> planning["2. Planning"]
    planning --> plan_review["3. Plan review"]
    plan_review --> implementation["4. Implementation"]
    implementation --> pr_review["5. PR review"]
    pr_review --> merge_wait["6. Merge wait"]
    merge_wait --> finished["7. Finished"]

    plan_review -. "nogo (iter 3 / plan_cycles 2)" .-> planning
    implementation -. "agent_error" .-> implementation
    pr_review -. "agent_error" .-> implementation
    implementation -. "already_implementation_go_pr" .-> merge_wait
```

Every back-edge in the diagram is **named** in
[`ROUTES`](hephaestus/automation/pipeline/routing.py) and is the "fail-route
reason vocabulary" stages must reference verbatim in `StageOutcome.note`.

### Coordinator / worker contract

The main thread (coordinator) OWNS:

- all seven stage queues ([`self.queues`](hephaestus/automation/pipeline/coordinator.py))
- the timer heap ([`self.timers`](hephaestus/automation/pipeline/coordinator.py))
- the in-flight registry ([`self.in_flight`](hephaestus/automation/pipeline/coordinator.py))
- all routing and disposition semantics ([`_route`](hephaestus/automation/pipeline/coordinator.py))
- every GitHub API mutation, through
 [`StageGitHub`](hephaestus/automation/pipeline/stages/base.py)
 (label writes, comment upserts, and PR creation; the queue does not mutate
 auto-merge)
It NEVER launches agents, builds/tests or git/network operations. It never
sleeps — wakeups are the timer's responsibility.
The single worker pool ([`WorkerPool`](hephaestus/automation/pipeline/worker_pool.py))
executes everything else: agent invocations (Claude or Codex), build/test
subprocesses and git/network operations. Every Claude agent invocation
routed through the worker pool binds to an explicit least-privilege
`--allowedTools` scope. An explicit [`AgentJob.allowed_tools`](hephaestus/automation/pipeline/jobs.py)
grant wins (the `pr_review` job uses it for the reviewer skill); absent that,
read-only sandbox jobs use [`DEFAULT_TOOL_SCOPE`](hephaestus/automation/pipeline/tool_scopes.py),
and all other jobs resolve through
[`tool_scope_for(agent)`](hephaestus/automation/pipeline/tool_scopes.py) from
[`AGENT_TOOL_SCOPES`](hephaestus/automation/pipeline/tool_scopes.py). An
unmapped role therefore falls through to the same read-only default rather
than the most permissive scope (#2319). Every git operation crosses
[`_repo_lock`](hephaestus/automation/pipeline/worker_pool.py) (in-process
thread lock, outer) **and**
[`_interruptible_file_lock`](hephaestus/automation/pipeline/worker_pool.py)
(cross-process flock, inner). Worktrees share `.git`, so two concurrent
operations on the same checkout would race.
The only cross-thread **payload** channel is the bounded
[`CompletionQueue`](hephaestus/automation/pipeline/queues.py)
(`queue.Queue[(JobHandle, JobResult)]`, capacity `C`). A separate
`threading.Event` latch is control-plane-only: workers set it after a
non-blocking completion publish, and signal handlers set it to wake an idle
coordinator. Neither writes a sentinel into the completion queue or blocks on
queue capacity
([`WorkerPool._on_future_done`](hephaestus/automation/pipeline/worker_pool.py),
[`Coordinator._wait_for_completion`](hephaestus/automation/pipeline/coordinator.py),
[`Coordinator._wake_completion_wait`](hephaestus/automation/pipeline/coordinator.py)).
The idle coordinator may wait on that event for its bounded poll interval
([`_IDLE_POLL_S = 1.0`](hephaestus/automation/pipeline/coordinator.py)); it
does not make a producer or signal handler wait. Pool size = `parallel_repos × max_workers`.
Each stage-queue capacity and the
completion-queue capacity are `C = max(1, parallel_repos × max_workers)`
([`_work_window`](hephaestus/automation/pipeline/coordinator.py),
[`WorkerPool(size=…)`](hephaestus/automation/pipeline/worker_pool.py)).

### Ticks

The per-tick event loop is defined in
[`Coordinator.run`](hephaestus/automation/pipeline/coordinator.py). One
tick does, in order:

1. **Shutdown check** — graceful drain or immediate teardown after the
 grace window / a second signal
 ([`_grace_exceeded`](hephaestus/automation/pipeline/coordinator.py),
 [`_immediate`](hephaestus/automation/pipeline/coordinator.py)).
2. **Wake timers** — pop every expired entry back into its stage queue
 ([`_wake_timers`](hephaestus/automation/pipeline/coordinator.py)).
3. **Drain completions** — handle ALL ready completions without blocking;
 interrupted results park the item RESUMABLE and never reach
 `on_job_done`
 ([`_drain_completions`](hephaestus/automation/pipeline/coordinator.py),
 [`_park_resumable`](hephaestus/automation/pipeline/coordinator.py)).
4. **Emit observability tick** — push queue-depth / in-flight / circuit
 breaker gauges and record alert transitions
 ([`_emit_observability_tick`](hephaestus/automation/pipeline/coordinator.py)).
5. **Drain queues down-stream first** — `finished → merge_wait →
 pr_review → implementation → plan_review → planning → repo`
 ([`_DRAIN_ORDER`](hephaestus/automation/pipeline/coordinator.py)).
 Implementation drains separately to enforce dependency topo-order
 and file-overlap serialization; other queues drain with the per-repo
 in-flight cap. Pending destination-first handoffs are retried before and
 between drains, and bounded direct/repository sources are admitted only at
 safe capacity points ([`_drain_implementation`](hephaestus/automation/pipeline/coordinator.py),
 [`_drain_queues`](hephaestus/automation/pipeline/coordinator.py),
 [`_drain_pending_handoffs`](hephaestus/automation/pipeline/coordinator.py),
 [`_drain_repo_issue_sources`](hephaestus/automation/pipeline/coordinator.py),
 [`_admit`](hephaestus/automation/pipeline/coordinator.py)).
6. **Idle-or-loop check** — if all queues + timers + in-flight are empty,
 re-seed up to `--loops` and either exit on zero work or continue
 ([`_all_idle`](hephaestus/automation/pipeline/coordinator.py),
 [`_reseed_if_converged`](hephaestus/automation/pipeline/coordinator.py)).
 Otherwise wait on the completion/signal latch and drain the bounded
 completion queue.
A defensive step watchdog ([`_STEP_WATCHDOG_S = 15.0`](hephaestus/automation/pipeline/coordinator.py))
warns when any `stage.step()` call exceeds ~15 s. 5 s proved too tight in
practice: routine repo-stage steps (clone + label reads over the network)
breached it on nearly every multi-repo run, burying real stalls in noise
(#2247).

### Library → product layer boundary

[`hephaestus.automation`](hephaestus/automation/) is the product layer. The
base import surface (`import hephaestus`) MUST NOT pull `curses`, `fcntl`,
`pydantic` or any `hephaestus.automation.*` module. Library subpackages
therefore cannot import `hephaestus.automation`.

---

## 3. Cross-cutting invariants

These invariants apply to **every** stage. Each stage section below cites
back to them.

### Journal-order invariant: durable write BEFORE the queue push

Every durable GitHub mutation (label add / remove / edit, comment upsert,
PR create) happens IMMEDIATELY BEFORE the
`StageOutcome` that causes the queue push. Restart then re-runs the stage
and the stage's idempotency checks (at-or-past label comparison, plan
comment presence, PR existence) fast-forward through already-completed
work. Interrupts therefore leave items RESUMABLE, never FAILED — a restart's
seeding classifies them back into the same entry queue and `on_enter`
restarts from the same state.
Implementation: each stage method performs its durable op via a single
[`ctx.github`](hephaestus/automation/pipeline/stages/base.py) accessor call
on the coordinator-owned [`StageGitHub`](hephaestus/automation/pipeline/stages/base.py)
protocol, then returns `StageOutcome(…)`. The coordinator's
[`_route`](hephaestus/automation/pipeline/coordinator.py) applies the
disposition to the queue.

### Global capacity, leases, and source cursors

Let `C = max(1, parallel_repos × max_workers)`. `C` is a global live-work
window, not a per-stage concurrency target. Every stage queue and the
completion queue have capacity `C`, while the coordinator holds one global
permit for every nonterminal `WorkItem`. Consequently, seven stage queues do
not permit `7C` simultaneous work items: an item holds the same permit as it
moves through the pipeline and releases it only after `finished` records its
outcome.

Queue draining claims an item through a
[`StageQueueLease`](hephaestus/automation/pipeline/queues.py). The lease keeps
the source queue slot occupied while a stage runs. A transition is
destination-first: the destination must accept the item before the source
lease is released and the item stage/history/result are changed. When the
destination is full, the coordinator retains exactly one pending handoff on
that source lease and retries it after downstream drains. The completed stage
action is therefore not replayed, and a full destination is ordinary
backpressure rather than a spill list, shutdown, or failure
([`StageQueueLease.handoff`](hephaestus/automation/pipeline/queues.py),
[`Coordinator._handoff_item`](hephaestus/automation/pipeline/coordinator.py),
[`Coordinator._drain_pending_handoffs`](hephaestus/automation/pipeline/coordinator.py)).
Leases carry stable FIFO tickets, so multiple ready items can be claimed and
run concurrently up to `C`; a retry restores ahead of later-admitted work
without serializing the whole stage.

Intake is also source-driven rather than an eager list of classified products:

- Explicit `--issues` and `--prs` use one cursor each. The coordinator
  classifies the next value only after every possible entry queue and the
  global live-work window can accept it; unadmitted caller input remains in
  the iterator, not in a `SeedEntry` or `WorkItem` spill buffer.
- Repository intake uses a FIFO repository source. Once a repo has prepared
  its checkout, its `RepoIssueSource` retains at most one pending metadata row
  and one fetched GitHub page. The coordinator keeps at most `C` active repo
  sources and gives each one admission attempt per FIFO round-robin, so a large
  repository cannot monopolize discovery.
- Organization repositories and linked-issue metadata use REST pages of at
  most 100 rows. Pagination continues until the short terminal page; there is
  no `gh ... --limit 500` discovery cap. An organization invocation passes a
  resettable paged repository iterator directly to the FIFO source; it never
  materializes the organization's names in CLI scope resolution or pipeline
  configuration. Each active repository cursor consumes issue metadata lazily.
  Repository
  discovery does not pre-scan open-PR pages: PR review context enters through
  the linked issue's classification. An orphan PR has no issue requirements
  and remains outside this source; an explicit `--prs` scope can select one
  for fail-closed direct evaluation, but cannot supply missing requirements.

The implementation is in
[`Coordinator._drain_direct_issue_source`](hephaestus/automation/pipeline/coordinator.py),
[`Coordinator._drain_repo_entry_source`](hephaestus/automation/pipeline/coordinator.py),
[`Coordinator._drain_repo_issue_sources`](hephaestus/automation/pipeline/coordinator.py),
[`RepoIssueSource`](hephaestus/automation/pipeline/stages/repo.py), and
[`loop_repo_manager`](hephaestus/automation/loop_repo_manager.py).

### Non-blocking retry / timer-park contract

Stages never sleep — the coordinator's timer heap owns every delay.
When a stage wants to wait (typically on a CI poll), it writes the delay
into `item.payload["retry_delay_s"]` and returns
`StageOutcome(Disposition.RETRY, note)`. The coordinator's
[`_route_retry`](hephaestus/automation/pipeline/coordinator.py) reads that
key and parks the item on the heap
([`_timer_park`](hephaestus/automation/pipeline/coordinator.py)).
A missing key means "retry on the next drain tick" (no delay).
[`BACKOFF_CAP_S = 60`](hephaestus/automation/pipeline/stages/base.py) is
shared by every stage that uses the legacy exponential poll delay.
Timer parking releases the source-stage lease but retains the item's global
work permit. On expiry, the timer heap remains the item's owner until its
stage queue accepts it; an occupied stage queue leaves the expired entry at
the heap head for a later tick. That is bounded backpressure, not an overflow
or a reason to repeat the delayed stage action
([`_timer_park`](hephaestus/automation/pipeline/coordinator.py),
[`_wake_timers`](hephaestus/automation/pipeline/coordinator.py)).

### Interrupt semantics

`Coordinator.run` installs SIGINT, SIGTERM, SIGHUP handlers
([`_install_signal_handlers`](hephaestus/automation/pipeline/coordinator.py)).
A first signal sets `shutdown` and starts a graceful drain window
([`_DEFAULT_GRACE_S = 30.0`](hephaestus/automation/pipeline/coordinator.py)).
The coordinator stops admitting new work, drains in-flight to RESUMABLE and parks touched items at their current stage. A second signal or an
expired grace window, tears the pool down immediately and the coordinator
synthesizes interrupted results for remaining in-flight jobs.
Items touched by an interrupt report
`ItemResult(passed=False, reason="resumable at <stage>", …)` — **never** FAILED. The
end-of-run summary lists them under `RESUMABLE at <stage>`. Resume is
label/PR/worktree reconstruction: rerunning the same scoped command
re-seeds each item into the correct entry queue. There is no persisted
queue snapshot.
The signal handler only sets the shutdown and wake latches; it never writes
to, or waits on, the bounded completion queue. Completion-queue saturation is
an internal invariant violation: the worker callback records a latch without
blocking or spilling, the coordinator fails the run, and still-live work is
finalized resumably. It does not set `shutdown` and does not turn into exit
code 130 ([`WorkerPool._on_future_done`](hephaestus/automation/pipeline/worker_pool.py),
[`Coordinator._drain_completions`](hephaestus/automation/pipeline/coordinator.py)).

### Exit codes

- `130` — interrupted by SIGINT, SIGTERM, or SIGHUP.
- `1` — any effective item failed, skipped, blocked; or the coordinator hit a fatal error.
- `0` — clean run.
[`_exit_code`](hephaestus/automation/pipeline/coordinator.py) deliberately
gives `130` priority over non-passing ledger entries and fatal coordinator
errors: a signal means the run did not complete.

### Effective-item rule

The summary uses `latest_logical_items(self.items)` from
[`summary.py`](hephaestus/automation/pipeline/summary.py) so a re-seeded
item's superseded attempts are collapsed before per-row / exit-code /
preserved-worktree calculation. The current item's own failed, skipped or blocked result still counts; an old failed attempt that was superseded
by a later passing attempt does not. Pull requests already merged/closed are
terminalized before summary collapse so stale attempts cannot re-enter the queue.

### Rate-budget gate

The legacy `_maybe_sleep_for_rate_budget` SLEEPS its loop thread — fatal
for a single coordinator thread. The new gate lives at the submit
chokepoint ([`_submit`](hephaestus/automation/pipeline/coordinator.py)):
[`_rate_budget_ok`](hephaestus/automation/pipeline/coordinator.py) calls
[`hephaestus.automation.pipeline_github.rate_budget_ok`](hephaestus/automation/pipeline_github.py)
and timer-parks an `AgentJob` until the upstream reset when the GraphQL
budget is low. Git/build jobs are unaffected. No `time.sleep` lives in any
stage module.

### Dry-run

When `--dry-run` is set, the coordinator:

- logs would-submit job descriptions and ADVANCEs the item
 ([`_run_item`](hephaestus/automation/pipeline/coordinator.py));
- asserts no job is EVER submitted
 ([`_submit`](hephaestus/automation/pipeline/coordinator.py));
- log-and-skip mutators in
 [`StageGitHub`](hephaestus/automation/pipeline/stages/base.py);
- finishes items instead of parking on RETRY with `retry_delay_s` (the
 preview will never see real-world CI / merge progress)
 ([`_route_retry`](hephaestus/automation/pipeline/coordinator.py));
- finishes items instead of failing back on FAIL_BACK (a dry-run mutator
 never writes the labels an earlier stage would re-check, so a regression
 would ping-pong until the safety cap)
 ([`_route_fail_back`](hephaestus/automation/pipeline/coordinator.py)).
The fleet-sync `--dry-run` is also a preview contract (see
[`AGENTS.md`](../AGENTS.md) §"Claude non-interactive permission policy").

### Poisoned-item fail-safety

Every `_run_item` call is wrapped in a per-item `try/except`; an item that
raises an unhandled exception inside a stage accessor is logged and routed
to [`FINISH_FAIL`](hephaestus/automation/pipeline/routing.py) instead of
terminating the loop, so one bad item never poisons the whole run (#2295).
Equivalently, when a `scope.trimmed_routes()` rewrite or a stage's own
`ROUTES` row has no `next`/`fail` mapping, the item lands at the next valid
mapping or `finished(fail)` rather than raising `KeyError`.

### Closed-schema stage events

Stage-originated JSONL events use the closed schema in
[`events.py`](hephaestus/automation/pipeline/events.py). The event surface is
intentionally minimal: `encode_stage_event` currently rejects every event, so
no stage event can carry reviewer text, GitHub bodies, or authorization facts.

### Scope trimming

[`PipelineScope`](hephaestus/automation/pipeline/routing.py) lets the
coordinator route items through a contiguous subset of stages
(`hephaestus-plan-issues` runs `planning → plan_review`;
`hephaestus-implement-issues` runs `implementation → pr_review`).
`hephaestus-merge-prs` is the manual merge-driving command outside the queue
coordinator (see [`hephaestus.github.pr_merge`](hephaestus/github/pr_merge.py)). `trimmed_routes()` rewrites every out-of-scope next/fail
target to `FINISHED`, so the partial route table is closed under
`scope ∪ {FINISHED}`. The coordinator always re-adds the universal sink:
see [`_routes = config.scope.trimmed_routes()`](hephaestus/automation/pipeline/coordinator.py).
`--force` on the planner CLI re-routes any at-or-past-scope stage back to
the scope's first stage so the scoped work is redone
([`_scope_seed_decision`](hephaestus/automation/pipeline/coordinator.py)).

### Cross-stage ping-pong bound

Some regression edges (`pr_review → implementation` for `agent_error`)
can ping-pong. The
[`_FAIL_BACK_CAP`](hephaestus/automation/pipeline/coordinator.py)
constant is the sum of every budget in
[`ROUTES`](hephaestus/automation/pipeline/routing.py). Stages enforce the
real per-key budgets themselves; the safety cap only guarantees
cross-stage cycles terminate even if a stage has a budget bookkeeping bug
([`_route_fail_back`](hephaestus/automation/pipeline/coordinator.py)).

---

## 4. WorkItem and the durable journal

### [§`WorkItem`](hephaestus/automation/pipeline/work_item.py)

The single per-item record moving through the queue. Thread-safety is by
construction: a `WorkItem` and its `StageQueue` are only ever touched by
the coordinator thread; the only cross-thread payload channel is the bounded
[`CompletionQueue`](hephaestus/automation/pipeline/queues.py). Event latches
carry wake/fault signals only, never `WorkItem` or `JobResult` payloads.
Key fields:

- `repo`, `kind` ([`ItemKind`](hephaestus/automation/pipeline/work_item.py)) —
 repo / issue / PR.
- `issue` (optional), `pr` (optional) — the GitHub identifier.
- `stage` ([`StageName`](hephaestus/automation/pipeline/routing.py)) —
 current queue.
- `state` — stage-local mini-state string (never a label).
- `attempts` — `dict` keyed by ROUTES budget names. Per-item-lifetime
 counter; never reset when an item re-enters a stage
 ([`_default_attempts`](hephaestus/automation/pipeline/work_item.py),
 [`routing.py`](hephaestus/automation/pipeline/routing.py) module
 docstring).
- `history` — `deque[HistoryEvent]` capped at
 [`HISTORY_CAP = 200`](hephaestus/automation/pipeline/work_item.py).
- `session_ids` — `dict[str, str]`, populated by agent invocations.
- `labels_cache` — last-known diagnostic label set. Planning and plan-review
 transition gates require a fresh GitHub read; cached labels never authorize
 advancement.
- `payload` — `dict[str, Any]`. The stage-local scratchpad for cross-step
 handoff (`retry_delay_s`, base-captured `base_branch`, validated audit facts,
 and process-owned thread receipts). It is not a durable authorization
 channel.
- `result` ([`ItemResult`](hephaestus/automation/pipeline/work_item.py)) —
 final `passed / reason / final_stage` written by
 [`_finish`](hephaestus/automation/pipeline/coordinator.py).
- `worktree`, `branch` — populated by [`implementation`](hephaestus/automation/pipeline/stages/implementation.py).

### [§`StageName`](hephaestus/automation/pipeline/routing.py)

`str`-flavored `Enum`:

```
REPO → PLANNING → PLAN_REVIEW → IMPLEMENTATION → PR_REVIEW →
 MERGE_WAIT → FINISHED
```

Declaration order matches
[`PIPELINE_ORDER`](hephaestus/automation/pipeline/routing.py) and the
[`_DRAIN_ORDER`](hephaestus/automation/pipeline/coordinator.py) reversed.
DO NOT REORDER — the `PipelineScope` contiguity check indexes by position.

### [§`Disposition`](hephaestus/automation/pipeline/routing.py)

`str`-flavored `Enum` returned in
[`StageOutcome.disposition`](hephaestus/automation/pipeline/routing.py):

- `ADVANCE` — route to `ROUTES[stage].next`.
- `RETRY` — read `payload["retry_delay_s"]`, timer-park (or re-push if
 missing) to `stage`.
- `FAIL_BACK` — reason-keyed regression via
 `ROUTES[stage].fail_routes.get(note, …)`; failing-back from the
 coordinator's safety cap finishes failed.
- `SKIP` — finish failed with reason `skip:<note>`.
- `BLOCKED` — finish failed with reason `blocked:<note>`.
- `FINISH_PASS` / `FINISH_FAIL` — terminal; pass with reason `<note>` /
 fail with reason `<note>`.
The disposition funnel is exhaustive: every layer in
[`Disposition`](hephaestus/automation/pipeline/routing.py) has a branch
in [`_route`](hephaestus/automation/pipeline/coordinator.py), so a new
value would be a static `TypeError` and a safe routing table edit.

### State-label vocabulary

Defined in [`state_labels.py`](hephaestus/automation/state_labels.py) and
imported throughout the pipeline. Seven labels: four mutually exclusive
planning states, two mutually exclusive implementation-review states, and one
absolute operator state:

| Label | Group | Authoritative stage |
|--------------------------------|--------------|---------------------------------|
| `state:needs-plan` | planner-scope| [`planning.on_enter`](hephaestus/automation/pipeline/stages/planning.py) |
| `state:plan-no-go` | planner-scope| [`plan_review._eval`](hephaestus/automation/pipeline/stages/plan_review.py) |
| `state:plan-go` | planner-scope| [`plan_review._eval`](hephaestus/automation/pipeline/stages/plan_review.py) |
| `state:plan-blocked` | planner-scope| [`plan_review._eval`](hephaestus/automation/pipeline/stages/plan_review.py) |
| `state:implementation-no-go` | review-scope | [`pr_review._eval`](hephaestus/automation/pipeline/stages/pr_review.py) |
| `state:implementation-go` | review-scope | [`pr_review._eval`](hephaestus/automation/pipeline/stages/pr_review.py) — **sole authority** |
| `state:skip` | absolute | operator / exhaustion in [`pr_review`](hephaestus/automation/pipeline/stages/pr_review.py) / [`implementation`](hephaestus/automation/pipeline/stages/implementation.py) |

Every **stage-issued** `state:skip` durable write (the `pr_review` and
`implementation` write paths, plus repo-stage epic tagging) has a
best-effort `gh_issue_upsert_comment` companion produced via
[`format_skip_reason_comment`](hephaestus/automation/state_labels.py), using
the [`SKIP_REASON_MARKER`](hephaestus/automation/state_labels.py) prefix
`<!-- hephaestus-state-skip-reason -->` so the reason survives outside the
run log (#2264). Epic tagging in
[`repo._seed_pass`](hephaestus/automation/pipeline/stages/repo.py) is the
sole sanctioned seeding write: it adds both the skip label and this comment
before excluding the epic from the rest of the pipeline.

Label colors per [`STATE_LABEL_SPECS`](hephaestus/automation/state_labels.py).
Provisioning script
([`hephaestus-ensure-state-labels`](scripts/)) creates them on a repo.

#### Ordered rank (`_LABEL_RANK`)

Used by [`seeding.py`](hephaestus/automation/pipeline/seeding.py) and
[`planning.on_enter`](hephaestus/automation/pipeline/stages/planning.py).
**NEVER use equality.** The at-or-past comparison is the only read the
gate trusts:

```
needs-plan : 0
plan-no-go : 1
plan-go : 2
implementation-no-go: 3
implementation-go : 4
state:skip : NO RANK (excluded from rank compare)
```

A label alone never authorizes merge. `merge_wait` requires both the
`state:implementation-go` label and a matching in-memory reviewed-head proof
on an open `main`, confirmed-unarmed live PR with an exclusive GO label. A
missing or drifted proof returns to review without a label mutation; a matching
proof permits a bounded sequence (default: five) of individual
SHA-conditional ordinary REST squash-merge requests. Before a request, a
read-only, per-reviewed-head readiness wait may park for up to 15 minutes;
readiness never authorizes merging, and fresh admission precedes every request
([`merge_wait.py`](hephaestus/automation/pipeline/stages/merge_wait.py)).

Plan-review labels are the sole durable authority. Review comments explain and
audit a decision but never authorize a transition, block a stage, or backfill a
missing label. Comment markers locate actor-owned journal artifacts only;
foreign marker text is ignored.

Implementation reviewers emit a structural audit, not a textual decision.
The host derives any implementation-state transition from that audit and
current GitHub facts, then confirms the relevant GitHub `state:*` label. Text
such as `Verdict: GO` or `Verdict: NOGO` is rejected as an implementation
review decision and cannot authorize, block, or backfill a transition. Plan
review instead ends with its explicit `state:plan-*` journal token. On restart,
seeding reads labels and PR state, never reviewer decision prose.

---

## 5. The seven queue stages

The seven stages are architectural responsibilities, not implementation
modules. This section describes boundaries, durable state, and transitions.
Worker types, helper functions, payload fields, and source-code structure are
intentionally omitted.

### Queue block diagram

```mermaid
flowchart LR
    GH["GitHub issue or PR"] --> R["1. Repo intake"]
    R --> P["2. Planning"]
    P --> V["3. Plan review"]
    V --> I["4. Implementation"]
    I --> Q["5. PR review"]
    Q --> M["6. Merge wait"]
    M --> F["7. Finished"]

    V -. "revision needed" .-> P
    V -. "external intervention needed" .-> H["Operator or dependency"]
    H -. "operator replaces blocked label" .-> P
    Q -. "changes needed" .-> I
    M -. "approval invalidated" .-> Q
```

GitHub is the durable journal. After a restart, labels, issue comments, pull
request reviews, review threads, and merge state reconstruct where work should
resume.

### 5.1 Repo intake

Repo intake discovers candidate work through a bounded source, records
exclusions, and routes each eligible issue or pull request to the stage implied
by its durable state.

#### Boundary diagram

```mermaid
flowchart LR
    Repository --> Discover --> Source["Bounded issue source"]
    Source --> Classify
    Classify -->|"eligible"| WorkItems["One routed work item"]
    Classify -->|"excluded"| DurableSkip["Durable skip state"]
    WorkItems --> Queues["Downstream queues"]
    DurableSkip --> Complete
```

#### State machine

```mermaid
stateDiagram-v2
    [*] --> Prepare
    Prepare --> Discover: repository available
    Prepare --> Prepare: transient failure
    Prepare --> Failed: retry limit reached
    Discover --> Source: source initialized
    Source --> Classify: one candidate admitted
    Discover --> Failed: discovery failed
    Classify --> RecordExclusions: exclusions found
    Classify --> Dispatch: no exclusions
    RecordExclusions --> Dispatch: exclusions durable
    Dispatch --> Source: next candidate
    Source --> Complete: cursor exhausted
    Complete --> [*]
    Failed --> [*]
```

Architectural contract:

- Exclusions become durable before excluded work leaves the queue.
- Discovery never writes planning, review, implementation, or merge verdicts.
- Failure of the repository item does not fabricate outcomes for its issues.
- Runtime repository discovery does not eagerly build `products` or downstream
  `WorkItem` lists. It fetches one issue page at a time, holds at most one
  pending metadata row per active repository cursor, and drains active cursors
  in FIFO round-robin order.

### 5.2 Planning

Planning produces one canonical implementation plan from the issue and its
durable chronological journal. The GitHub journal remains complete for audit
and recovery. A resumed rejected plan receives only bounded context for its
current canonical plan and paired current review; superseded revisions do not
compete with the active critique. A blocked plan is an automation stop: only
an external actor may resolve the dependency and replace `state:plan-blocked`
with exactly one next plan-state label.

#### Boundary diagram

```mermaid
flowchart LR
    Issue["Issue text"] --> Context
    History["Current rejected plan/review"] --> Context
    Context --> Planner --> Canonical["Canonical plan comment"]
    Canonical --> PlanReview["Plan review"]
```

#### State machine

```mermaid
stateDiagram-v2
    [*] --> Eligibility
    Eligibility --> Skipped: excluded or already implemented
    Eligibility --> Ready: approved plan exists
    Eligibility --> AwaitOperator: state:plan-blocked present
    Eligibility --> BuildContext: eligible plan-state label
    AwaitOperator --> Eligibility: external actor replaces blocked label
    BuildContext --> Draft
    Draft --> Verify: candidate produced
    Draft --> Draft: recoverable failure
    Verify --> Publish: candidate complete
    Verify --> Draft: candidate unusable
    Verify --> Failed: retry limit reached
    Publish --> Ready: canonical plan updated; label transition confirmed
    Ready --> [*]
    Skipped --> [*]
    Failed --> [*]
```

Architectural contract:

- The first automation journal role is the canonical implementation plan,
  identified by an opaque marker and updated only by its owning GitHub actor.
- A restarted rejected plan receives only its bounded current canonical plan
  and paired current review. Superseded revision comments remain audit and
  recovery artifacts and are not active planner context.
- Journal ingestion is bounded by both comment count and body bytes. If either
  limit is exceeded, automation stops with an explicit manual-recovery error
  instead of silently dropping old plan/review revisions.
- Historical revision comments are actor-owned, append-once, and never edited
  or deleted.
- Each durable state transition is published with its corresponding canonical
  artifact, and restart routing reads the label rather than comment prose.
- `state:plan-blocked` is never removed or replaced by automation. Comments do
  not revive it. After resolving the dependency, an external actor sets exactly
  one next state: ordinarily `state:plan-no-go` to request amendment,
  `state:plan-go` to approve, or `state:needs-plan` only when no canonical plan
  should be reused.

### 5.3 Plan review

Plan review decides whether the canonical plan is ready, can improve through a
bounded revision, or needs external intervention. The GitHub issue label is the
sole durable authority: `state:plan-go`, `state:plan-no-go`, or
`state:plan-blocked`. Reviewer output proposes one of those labels, but routing
occurs only after GitHub confirms the label write. The canonical review remains
an audit record and context source, never an authorization fallback.

#### Boundary diagram

```mermaid
flowchart LR
    Plan["Canonical plan"] --> Review
    History["Current plan and direct critique"] --> Review
    Review -->|"plan-go / plan-no-go"| CanonicalReview["Canonical review comment"]
    CanonicalReview --> Label["Confirmed GitHub plan-state label"]
    Review -->|"plan-blocked"| BlockLatch["Confirmed blocked label"]
    BlockLatch --> BlockAudit["Canonical blocked explanation"]
    Label -->|"plan-go"| Implementation
    Label -->|"plan-no-go"| Archive["Archive prior plan and review"]
    Archive --> Planning
    BlockAudit --> External["Human or dependency"]
```

#### State machine

```mermaid
stateDiagram-v2
    [*] --> ReconcileJournal
    ReconcileJournal --> LoadHistory: journal complete
    ReconcileJournal --> ReconcileJournal: interrupted archive recovered
    ReconcileJournal --> Failed: conflicting actor-owned immutable artifact
    LoadHistory --> Review: context available
    LoadHistory --> Failed: context unavailable
    Review --> RetryReview: invalid reviewer response
    RetryReview --> Review: retry available
    RetryReview --> Failed: reviewer failures exhausted
    Review --> PublishAudit: plan-go or plan-no-go proposal
    Review --> ApplyBlocked: plan-blocked proposal
    ApplyBlocked --> RetryReview: blocked label write or confirmation failed
    ApplyBlocked --> PublishBlockedAudit: state:plan-blocked confirmed
    PublishBlockedAudit --> Blocked: explanation stored; latch remains on audit failure
    PublishAudit --> ApplyLabel: review comment stored
    ApplyLabel --> RetryReview: label write or confirmation failed
    ApplyLabel --> Approved: state:plan-go confirmed
    ApplyLabel --> AssessRevision: state:plan-no-go confirmed
    AssessRevision --> ArchiveRevision: improvement remains possible
    AssessRevision --> Blocked: no improvement and planning is stuck
    AssessRevision --> Blocked: external decision or dependency required
    AssessRevision --> Failed: revision limit reached
    ArchiveRevision --> Amend: prior plan archived with recovery payload
    Amend --> PublishRevision: revised plan produced
    PublishRevision --> Review: canonical comments updated
    Approved --> [*]
    Blocked --> [*]
    Failed --> [*]
```

Architectural contract:

- The second automation journal role is the canonical plan review, identified
  by an opaque marker and updated only by its owning GitHub actor.
- Marker collisions authored by other actors are inert. They cannot become
  canonical artifacts, establish replay identity, or stop an owned write.
- Before canonical comments change, the previous plan and its review are
  appended as immutable chronological comments.
- The plan archive contains the next-plan recovery payload. On restart, a
  missing review archive or canonical update is completed idempotently before
  another agent runs.
- Review receives the current plan and direct prior critique. An amendment
  receives the bounded current canonical plan and direct critique; superseded
  journal revisions remain durable audit and recovery artifacts, not prompt
  context.
- A blocked verdict states exactly what decision or dependency is required.
  Because BLOCKED is the safety latch, its label is confirmed before the
  fallible explanatory write; an audit-write failure can never leave the item
  eligible for autonomous work.
  On restart, automation may repair a missing explanation with a generic,
  actionable audit comment, but it does not change the blocked label or invoke
  a planning agent.
  Automation remains stopped until an external actor resolves it and replaces
  the blocked label; a comment by itself has no routing effect.
- No-improvement detection exits early as blocked instead of spending further
  planning iterations.
- Invalid reviewer output retries review without consuming a plan revision.

### 5.4 Implementation

Implementation converts an approved plan into a published pull request. It may
adopt an existing pull request, but it cannot approve its own work or authorize
a merge.

#### Boundary diagram

```mermaid
flowchart LR
    Plan["Approved plan"] --> Implement
    ExistingPR["Existing PR"] --> Adopt
    Adopt --> Workspace
    Implement --> Workspace
    Workspace --> Changes --> Publish["Branch and PR"]
    Publish --> PRReview["PR review"]
```

#### State machine

```mermaid
stateDiagram-v2
    [*] --> Admission
    Admission --> Skipped: issue excluded
    Admission --> PlanReview: approval missing or revoked
    Admission --> InspectPR: eligible
    InspectPR --> Prepare: no open PR
    InspectPR --> Adopt: open PR exists
    InspectPR --> Complete: already merged
    InspectPR --> Failed: closed without merge
    Adopt --> Prepare: safe to continue
    Adopt --> Failed: unsafe adoption
    Prepare --> Implement: workspace ready
    Prepare --> Failed: workspace unavailable
    Implement --> Publish: changes complete
    Implement --> Implement: recoverable failure
    Implement --> Failed: retry limit reached
    Publish --> ReviewReady: branch and PR durable
    Publish --> Publish: transient publication failure
    Publish --> Failed: publication failed
    ReviewReady --> [*]
    PlanReview --> [*]
    Complete --> [*]
    Skipped --> [*]
    Failed --> [*]
```

Architectural contract:

- One issue maps to one active implementation pull request.
- The queue observes but never mutates auto-merge while review is pending.
- Implementation never writes `state:implementation-go`.
- Missing approval returns to plan review; unsafe or exhausted work terminates
  without approval.

### 5.5 PR review

PR review is the sole authority for implementation approval. Reviews and
findings are recorded on the GitHub pull request so their history survives
local process or agent-session loss.

#### Boundary diagram

```mermaid
flowchart LR
    PR["PR diff and requirements"] --> Review
    Review --> GitHub["GitHub review and inline threads"]
    GitHub --> Gate{"Blocking findings?"}
    Gate -->|"automation-owned"| Address --> PublishFix --> Review
    Gate -->|"human-owned"| Human["Human action"]
    Gate -->|"none"| Approve["state:implementation-go"]
    Approve --> MergeWait
```

#### State machine

```mermaid
stateDiagram-v2
    [*] --> VerifyUnarmed
    VerifyUnarmed --> Review: open, complete, and unarmed
    VerifyUnarmed --> OperatorOwned: external arm or incomplete state
    Review --> Checkout: GitHub snapshot captured
    Checkout --> Review: clean checkout matches snapshot head
    Checkout --> RetryReview: checkout or head drift
    RetryReview --> Review: bounded retry
    Review --> Validate: review produced
    Review --> RetryReview: invalid output
    RetryReview --> Review: retry available
    RetryReview --> Implementation: fresh implementation context required
    Validate --> Post: findings normalized
    Post --> Evaluate: GitHub review recorded
    Evaluate --> HumanBlocked: human-owned finding
    Evaluate --> Address: blocking automation finding
    Evaluate --> ResolveMinor: advisory findings only
    Evaluate --> Approve: no unresolved findings
    Evaluate --> Skipped: no progress or review limit reached
    Address --> PublishFix: changes produced
    PublishFix --> Review: fixes published
    ResolveMinor --> Approve
    Approve --> MergeReady: implementation-go durable
    MergeReady --> [*]
    HumanBlocked --> [*]
    Implementation --> [*]
    Skipped --> [*]
    Failed --> [*]
```

Architectural contract:

- Every implementation review is posted to the pull request.
- Actionable findings use durable inline threads and severity.
- Prior rounds remain visible in the PR timeline.
- Blocking findings produce `state:implementation-no-go`; only a clean review
  produces `state:implementation-go`.
- Human-owned findings stop automation with an explanatory PR comment.
- The review proof is a fresh GitHub snapshot plus a clean checkout at that
  snapshot's head; it exists only in the current process.
- No queue stage arms, disables, adopts, or polls auto-merge.

### 5.6 Merge wait

Merge wait verifies a still-valid implementation approval against its
in-memory reviewed-head proof before each request. It may issue a bounded
sequence (default: five) of individual ordinary REST squash-merge requests,
each conditional on that SHA. Admission for every request requires an open
`main` PR, an explicitly unarmed record, and an exclusive implementation-GO
label. A read-only readiness wait may park for up to 15 minutes per reviewed
head before a request, without consuming the merge budget or authorizing a
merge. The direct adapter performs one request per call and never retries.
Merge wait does not invoke `gh pr merge`,
create, disable, adopt, or poll native auto-merge, manage a merge queue, or use
an administrator bypass; an existing request is external ownership and is left
untouched.

#### Boundary diagram

```mermaid
flowchart LR
    Approved["Approval label + reviewed-head proof"] --> Verify
    Verify --> Merge["Conditional SHA squash merge"]
    Verify --> Review["Missing or drifted proof"]
    Verify --> Operator["External or ambiguous ownership"]
    Merge --> Merged --> Learn["Optional learning"] --> Finished
```

#### State machine

```mermaid
stateDiagram-v2
    [*] --> Inspect
    Inspect --> Complete: already merged
    Inspect --> Failed: closed or unavailable
    Inspect --> OperatorOwned: externally armed
    Inspect --> PRReview: approval missing
    Inspect --> Verify: approval label present
    Verify --> Merge: matching reviewed head, main, unarmed exclusive GO
    Verify --> PRReview: missing or drifted proof
    Verify --> OperatorOwned: externally armed or ownership ambiguous
    Verify --> Failed: incomplete or unavailable state
    Merge --> Complete: 200 merged and lifecycle confirms
    Merge --> PRReview: 409 or ambiguous lifecycle head drift
    Verify --> Retry: readiness pending
    Merge --> Retry: 405 race or safe ambiguous retry
    Retry --> Verify: timer (readiness deadline) or transport retry
    Learn --> Complete: disabled or recorded
    Learn --> Failed: durable outcome ambiguous
    Complete --> [*]
    PRReview --> [*]
    OperatorOwned --> [*]
    Failed --> [*]
```

Architectural contract:

- A current-process review proof is bound to the reviewed head commit.
- Existing external merge ownership is preserved.
- Missing or drifted proof returns approval to PR review with zero label writes.
- A matching proof can submit a bounded sequence of individual
  SHA-conditional normal REST merge requests, each only after fresh admission.
- Read-only readiness polling may wait up to 15 minutes per reviewed head
  without spending the request budget or authorizing a merge. HTTP 409,
  transport ambiguity, and every actual request remain subject to fresh
  lifecycle, head, label, thread, and protection checks.

### 5.7 `finished`

Finished records the final outcome exactly once and applies workspace retention
policy. It does not change issue, review, or merge verdicts.

States: `ENTER → RECORD → CLEANUP → DONE`.

#### Boundary diagram

```mermaid
flowchart LR
    Outcome --> Record --> Decision{"Passed?"}
    Decision -->|"yes"| Cleanup
    Decision -->|"no"| Preserve
    Cleanup --> Done
    Preserve --> Done
```

#### State machine

```mermaid
stateDiagram-v2
    [*] --> ENTER
    ENTER --> RECORD
    RECORD --> CLEANUP: result recorded
    CLEANUP --> DONE: remove passed workspace
    CLEANUP --> DONE: preserve failed workspace
    CLEANUP --> DONE: no workspace
    DONE --> [*]
```

Architectural contract:

- A terminal result is recorded once.
- Failed workspaces are preserved for diagnosis.
- Successful temporary workspaces are removed when safe.
- Cleanup failure never rewrites the underlying result.

---

## 6. The ROUTES table — single source of truth

[`ROUTES`](hephaestus/automation/pipeline/routing.py) (and its mirror in
[`docs/architecture.md`](architecture.md))
is the **single source of truth** for next-stage targets and per-stage
budgets. Every `routes.py` row and every doc row MUST agree.

| Stage | `next` (success) | Fail reasons → target | Budgets |
|-------------------|------------------|-------------------------------------------------------------|----------------------------|
| `repo` | `FINISHED` | `*` → `FINISHED` | `clone = 2` |
| `planning` | `PLAN_REVIEW` | `*` → `FINISHED` | `plan = 2` |
| `plan_review` | `IMPLEMENTATION` | `nogo` → `PLANNING`; `plan_cycles_exhausted` → `FINISHED`; `*` → `PLANNING` | `plan_review_iter = 3`, `plan_cycles = 2` |
| `implementation` | `PR_REVIEW` | `plan_not_go` → `PLAN_REVIEW`; `already_implementation_go_pr` → `MERGE_WAIT`; `*` → `FINISHED` | `implement = 2`, `test_fix = 1` |
| `pr_review` | `MERGE_WAIT` | `agent_error` → `IMPLEMENTATION`; `human_blocked` → `FINISHED`; `exhaustion` → `FINISHED`; `*` → `PR_REVIEW` | `pr_review_iter = 3`, `pr_review_hard = 6` |
| `merge_wait` | `FINISHED` | `not_implementation_go`, `reviewed_head_missing`, or `reviewed_head_drift` → `PR_REVIEW`; `closed` → `FINISHED`; `*` → `FINISHED` | `merge = 5` |
| `finished` | `FINISHED` | — (terminal) | — |

Budget provenance (cross-check):

- `plan_review_iter = 3`, `pr_review_iter = 3` ←
 [`_review_phase.py MAX_REVIEW_ITERATIONS`](hephaestus/automation/_review_phase.py)
 (the review-iteration cap; the value tag is the durable reference —
 line numbers drift).
- `pr_review_hard = 6` ←
 [`_review_phase.py MAX_REVIEW_ITERATIONS_HARD_CAP`](hephaestus/automation/_review_phase.py)
 (= 3 × 2, the progress-aware extension cap).
- `clone = 2`, `plan = 2`, `plan_cycles = 2`, `implement = 2`,
 `test_fix = 1`, `merge =
 DEFAULT_DRIVE_GREEN_LOOPS = 5` ←
 [`loop_runner.py LoopConfig.drive_green_loops`](hephaestus/automation/loop_runner.py).
- `merge = 5` (CLI default for `--drive-green-loops`,
 [`DEFAULT_DRIVE_GREEN_LOOPS`](hephaestus/automation/pipeline/routing.py))
 bounds queue `merge_wait` conditional requests and transport-ambiguity
 retries. Operational readiness waits use a separate 15-minute monotonic
 deadline keyed to the current reviewed head.
All per-item-lifetime counters live in
[`WorkItem.attempts`](hephaestus/automation/pipeline/work_item.py);
they are NEVER reset when an item re-enters a stage, so cross-stage
regression cycles (e.g. pr_review → implementation) remain
globally bounded.

---

## 7. Seeding and restart reconstruction

[`seeding.py`](hephaestus/automation/pipeline/seeding.py) is the pure
classifier the coordinator consults on every restart. It maps
`(labels, PR existence/state)` to a single entry stage using **ordered
label rank**:

```
needs-plan (0) < plan-no-go (1) < plan-go (2) <
 implementation-no-go (3) < implementation-go (4)
```

The at-or-past comparison is the only read the gates trust; equality
strands issues already past target.

### Tri-state PR fetch

[`seed_issue_from_github`](hephaestus/automation/pipeline/seeding.py)
(or its CLI counterpart
[`seed_issue`](hephaestus/automation/pipeline/seeding.py)) runs the
two-lookup PR fetch in a strict order: open first
([`find_pr_for_issue`](hephaestus/automation/pipeline/seeding.py)),
then merged ([`find_merged_pr_for_issue`](hephaestus/automation/pipeline/seeding.py)).
A closed PR is invisible to both lookups and is normalized to
`pr_number = None` — the classifier then ONLY ever sees a clean
`{no live PR | open PR | merged PR}` tri-state. Fail-closed: any GitHub
error from the issue fetch OR either PR lookup propagates (so a transient
PR-probe failure cannot misclassify toward IMPLEMENTATION).

### Classification table

| GitHub state | Entry stage |
|-------------------------------------------------------|----------------------------------|
| `state:skip`/`epic` | excluded (`stage = None`) |
| Direct PR already merged | `FINISHED` (pass, idempotent) |
| Direct PR already closed | excluded |
| Open PR carries `state:implementation-go` | `MERGE_WAIT` |
| Open PR with `state:implementation-no-go` | `PR_REVIEW` |
| Open PR, neither impl label | `PR_REVIEW` |
| No PR, at-or-past `state:plan-go` | `IMPLEMENTATION` |
| No PR, `state:plan-no-go` | `PLANNING` (amend path) |
| No PR, `state:plan-blocked` | excluded until an external actor resolves the block and replaces the label; comments alone are inert |
| No state label / `state:needs-plan` | `PLANNING` |

Epic tagging is the **ONE sanctioned seeding write**. GitHub mutations are
forbidden in `seeding.py`, so
[`EpicSkipTagObligation`](hephaestus/automation/pipeline/seeding.py)
is discharged by the coordinator through
[`ctx.github.skip_epics`](hephaestus/automation/pipeline/stages/base.py)
BEFORE the exclusion is honored
([`_seed_pass`](hephaestus/automation/pipeline/coordinator.py)).

### Seeding and re-seed scope

- `--repos` is consumed by a FIFO repository cursor; it creates repo work only
 when both the REPO queue and the global live-work window have capacity.
- `--issues` and `--prs` are direct bounded cursors. Each value is classified
 only when its eventual entry queue is guaranteed to accept it, so a large
 CLI scope is not converted into an eager seed list. `--prs` is the explicit
 route for a PR that is not reached from linked-issue discovery.
- `--org` is a resettable, paged GitHub REST repository source. It filters the
 `archived` and `fork` response fields before each name enters the FIFO source;
 repository names are not materialized up front. Linked-issue discovery uses
 100-row REST pages rather than a 500-item
 CLI limit; it does not bulk-scan every open PR
 ([`loop_repo_manager.py`](hephaestus/automation/loop_repo_manager.py)).
When `--issues` or `--prs` is set, the resolved `--repos` list is used
ONLY for context — repo discovery is NOT enqueued, so a scoped run
cannot reconstruct every open issue in the repo (deliberate scope
isolation).
After `coordinator._seed_pass`, if all queues, active leases, source cursors,
timers, and in-flight jobs are empty,
[`_reseed_if_converged`](hephaestus/automation/pipeline/coordinator.py)
re-seeds up to `--loops` and either exits on a zero-work pass or
continues.

### Merge-wait restart semantics

The queue is in-memory: a restart re-seeds normally through the ordinary
[`classifier`](hephaestus/automation/pipeline/seeding.py) and does not recover
the process-local reviewed-head proof. A direct PR seed or restart therefore
cannot use a durable implementation-GO label by itself: merge wait first
requires a confirmed-unarmed read, then returns the PR to review without
mutating its labels. Other-run auto-merge requests are
[blocked without adoption or mutation](hephaestus/automation/pipeline/stages/merge_wait.py)
and require operator handling.

---

## 8. The worker pool and job contract

[`WorkerPool`](hephaestus/automation/pipeline/worker_pool.py) is the
single executor. It receives frozen specs and returns bounded
[`JobResult`](hephaestus/automation/pipeline/jobs.py) tuples. Workers
never touch WorkItems or stage queues and never perform GitHub API
mutations.

### Job kinds

- [`AgentJob`](hephaestus/automation/pipeline/jobs.py) — Claude or
 Codex (`agent = resolve_agent(job.agent)`) with
 `prompt_builder(**prompt_kwargs)` composed in-worker.
 `resume_session_id`, when set for a direct runner, selects its persisted
 session instead of creating a fresh one; its returned id is carried in the
 `JobResult` and persisted by the coordinator under the job's logical role.
 `sandbox = "workspace-write"` (default) or `"read-only"` (including PR
 review). The agent job has no head-SHA field; the checkout barrier runs before
 it as a `verify_pr_review_checkout` Git job.
 `sandbox = "read-only"` activates `allowed_tools = "Read,Glob,Grep"`
 and `permission_mode = "dontAsk"` on the Claude call site.
- [`BuildTestJob`](hephaestus/automation/pipeline/jobs.py) — subprocess
 argv. Security: argv MUST NOT carry untrusted strings; only the
 coordinator constructs them from vetted templates
 (`PRE_PR_TEST_ARGV` for the pre-PR test gate).
- [`GitJob`](hephaestus/automation/pipeline/jobs.py) — `op ∈ {clone,
 sync_checkout, create_worktree, verify_pr_review_checkout, remove_worktree,
 rebase, push, commit_push}`, validated by `__post_init__`. Before a PR-review
 agent job, `verify_pr_review_checkout` receives the worktree path, branch,
 expected snapshot SHA, and PR number. The worker rejects a dirty checkout,
 synchronizes the branch, requires `git rev-parse HEAD` to equal that SHA, and
 checks cleanliness again ([`_git_verify_pr_review_checkout`](hephaestus/automation/pipeline/worker_pool.py)).
- [`CompactJob`](hephaestus/automation/pipeline/jobs.py) — a best-effort
 `/compact` turn for a persisted Claude, Codex, or Pi session; it never blocks
 the retry lifecycle.

### Result semantics

[`JobResult.ok = False, value = None, error`](hephaestus/automation/pipeline/jobs.py)
on any failure (return code != 0, `subprocess.TimeoutExpired`,
exception). Stdout/stderr tails are trimmed to 4 KiB in the `JobResult`;
the error message is truncated to 500 chars.

### Completion contract

Every non-cancelled `submit()` produces EXACTLY ONE
`(JobHandle, JobResult)` tuple on the completion queue
([`_on_future_done`](hephaestus/automation/pipeline/worker_pool.py)).
Normal job failures are converted to error results in `_run`; anything
that escapes `future.result()` (exception + process-control escapes
`KeyboardInterrupt`/`SystemExit`/`GeneratorExit`) is converted to a
`worker_crash` result so a non-cancelled submit never silently loses
its completion. Only futures cancelled before starting emit no
completion (the coordinator synthesizes those).

The completion queue is bounded to `C`, and the worker callback uses
`put_nowait`. Under the global permit invariant, every in-flight job has a
reserved completion slot. If that invariant is ever violated, the callback
sets a saturation latch and wakes the coordinator; it neither blocks nor keeps
an overflow buffer. The coordinator treats that latch as a fatal internal
fault and preserves remaining in-flight items as resumable work. Signal
handlers use the same wake mechanism but only a real OS signal sets shutdown
and selects exit code 130.

### Per-repo lock layering

[`_run_git`](hephaestus/automation/pipeline/worker_pool.py) wraps every
git operation in two locks:

1. **Outer**: in-process `threading.Lock` per repo ([`_repo_lock`](hephaestus/automation/pipeline/worker_pool.py))
 — single-thread per process serializes at most one thread per
 repo, sidestepping `flock`'s same-process ambiguity.
2. **Inner**: cross-process
 [`file_lock`](hephaestus/utils/file_lock.py) at
 `<repo_root>/<DEFAULT_STATE_DIR>/locks/git-<repo>.lock`
 ([`_repo_lock_path`](hephaestus/automation/pipeline/worker_pool.py))
 with a bounded wait using interruptible polling.
Both locks are held for the entire operation because worktrees share
`.git`.

`sync_checkout` additionally takes the status-safe Git-metadata lock resolved
by [`WorktreeManager.git_metadata_lock_path`](hephaestus/automation/worktree_manager.py).
For linked worktrees this resolves Git's common directory, so the primary
checkout and every linked worktree serialize synchronization and worktree
metadata mutations without leaving an untracked sentinel in the worktree.
Before inspecting the origin or worktree status, it also reads the effective
repository and worktree Git configuration with global/system configuration
disabled, rejecting executable, transport-routing, and TLS-affecting settings
from a reusable checkout.

### Resilience wiring

[`hephaestus.resilience.resilient_call`](hephaestus/resilience/__init__.py)
wraps agent invocation. The retry predicate is
`retry_predicate=lambda _exc: not self._shutdown.is_set()` — we accept
the cost of re-running the whole agent session on a transient blip
(network reset, gh flake) because agent invocations are
workflow-idsempotent (plan/review comments upsert; implementer re-runs
converge on the same branch). Non-transient errors (`rc != 0`, timeouts)
are NOT retried.

### Rate budget + timeout mapping

- `phase_timeout_s` (CLI `--phase-timeout`) bounds each AgentJob at
 [`_submit`](hephaestus/automation/pipeline/coordinator.py), not the
 whole phase subprocess.
- `agent_default_timeout()` / `planner_claude_timeout()` /
 `implementer_claude_timeout()` / `pr_reviewer_claude_timeout()` /
 `ci_driver_claude_timeout()` /
 `learn_claude_timeout()` /
 [`...`](hephaestus/automation/agent_config.py) are
 phase-specific CLI-time defaults; every per-phase timeout reads
 `HEPH_<PHASE>_AGENT_TIMEOUT` so operators can tune without code
 change.

---

## 9. Thin CLI scope wrappers and rollout controls

Five console scripts are thin queue-pipeline scoped entry points
(preserve their historical CLI surfaces). Manual merge-driving is
out-of-band.

| Console script | Stage slice | Entry module |
|--------------------------------------|-----------------------------------|---------------------------------------------------|
| `hephaestus-plan-issues` | `planning → plan_review` | [`planner`](hephaestus/automation/planner.py) |
| `hephaestus-implement-issues` | `implementation → pr_review` | [`implementer`](hephaestus/automation/implementer.py) |
| `hephaestus-review-prs` | `pr_review` (internal slice) | [`pr_reviewer`](hephaestus/automation/pr_reviewer.py) |
| `hephaestus-drive-prs-green` | `pr_review → merge_wait` | [`ci_driver`](hephaestus/automation/ci_driver.py) |
| `hephaestus-merge-prs` | (manual merge-driving, queues disabled) | [`hephaestus.github.pr_merge`](hephaestus/github/pr_merge.py) |
| `hephaestus-agent-stage` | (one-shot stage invocation) | [`agent_stage`](hephaestus/automation/agent_stage.py) |

`--run-pre-pr-tests` is an opt-in queue-runner flag enabling the
[`implementation`](hephaestus/automation/pipeline/stages/implementation.py)
pre-PR unit-test gate — argv comes from
`PipelineConfig.pre_pr_test_argv` so non-standard unit-test layouts
work without code change.

Three Codex-only flags control per-role reasoning effort:
`--planner-reasoning-effort {default|low|medium|high|xhigh}` and the
analogous `--reviewer-reasoning-effort` and `--implementer-reasoning-effort`
([`_build_parser`](hephaestus/automation/loop_runner.py)). A role-specific
value takes precedence over the selected model alias's `model_reasoning_effort`
default; `default` deliberately omits the setting so the alias keeps its
established baseline. These flags are applied only to the Codex provider
and never modify Claude or Pi model IDs (#2287).
The default pipeline accepts `--loops`, `--parallel-repos`,
`--max-workers` and per-agent `--agent` plus per-phase reasoning
controls:

- `--planner-reasoning-effort`
- `--implementer-reasoning-effort`
- `--reviewer-reasoning-effort`
Each takes `default | low | medium | high | xhigh`. `default` deliberately
omits Codex's `model_reasoning_effort` setting. When omitted the
selected model alias's default takes over. The values are injected
through [`stage_model`](hephaestus/automation/pipeline/stages/base.py),
which re-cuts the model id with `:effort` for Codex only.

---

## 10. Observability, dry-run and rate-budget gate

Observability is **opt-in**: it is built only when
`PipelineConfig.metrics_port > 0`. The coordinator imports
[`MetricsRegistry`](hephaestus/observability/metrics.py),
[`MetricsHTTPServer`](hephaestus/observability/server.py) and
[`AlertTracker`](hephaestus/observability/alerts.py) lazily inside
the constructor so the default construction path keeps its zero-I/O
import contract
([`Coordinator.__init__`](hephaestus/automation/pipeline/coordinator.py)).

### Bounded diagnostics and non-authoritative JSONL

The coordinator retains only finite local diagnostic state: the in-memory
event deque defaults to 1,024 records, detailed terminal items/ledger/
preserved-worktree entries default to 128, and the per-repository stage-context
cache is an LRU capped at `C`. A constant-space terminal summary still
aggregates the entire run's pass/fail totals after older details are trimmed
([`PipelineConfig.event_log_capacity`](hephaestus/automation/pipeline/coordinator.py),
[`PipelineConfig.terminal_detail_capacity`](hephaestus/automation/pipeline/coordinator.py),
[`Coordinator._record_terminal_result`](hephaestus/automation/pipeline/coordinator.py)).

When `event_log_path` is configured, the coordinator also appends diagnostic
records to JSONL. That file is best-effort: an I/O failure logs a
warning and disables further JSONL writes without changing pipeline routing.
It is not a queue snapshot or recovery journal. GitHub labels, comments, and
PR state remain the only restart authority.

### Gauges

[`_emit_observability_tick`](hephaestus/automation/pipeline/coordinator.py)
publishes the following gauges once per coordinator tick. Each gauge
retains its label series across ticks so a completed job or
state-transition is rendered as zero, not as stale active work.

| Gauge | Type | Labels | Default | Semantics |
|-------------------------------------------|--------|-----------|---------|-----------|
| `hephaestus_pipeline_queue_depth` | Gauge | `stage` | `0` | Item count per pipeline stage. Useful for detecting back-pressure. |
| `hephaestus_pipeline_inflight_jobs` | Gauge | (none) | `0` | Total in-flight jobs across all worker pools. |
| `hephaestus_pipeline_inflight_per_repo` | Gauge | `repo` | `0` | In-flight jobs by repo, capped by `max_workers`. |
| `hephaestus_circuit_breaker_state` | Gauge | `name`,`state` | `0` | `1` for the active state, `0` for prior states (only emitted from the optional `circuit_breaker_snapshot_provider`). |
| `hephaestus_pipeline_alert_active` | Gauge | `name` | `0` | `1` while a fired alert is unresolved, `0` when resolved. |

The `circuit_breaker_snapshot_provider` is **product-layer supplied**;
the coordinator never imports the resilience capability directly
([`PipelineConfig.circuit_breaker_snapshot_provider`](hephaestus/automation/pipeline/coordinator.py)).
A broken provider is swallowed by a `logger.exception` and treated as
"no breakers known this tick" — observability must NEVER be able to
terminate a production automation loop.

### AlertTracker behavior

[`AlertTracker.observe(snapshot)`](hephaestus/observability/alerts.py) is
called once per tick with the coordinator's
[`_observability_snapshot`](hephaestus/automation/pipeline/coordinator.py).
Emitted events drive `hephaestus_pipeline_alert_active` and a best-effort
`alert_<fired|resolved>` diagnostic event-log entry.

- **Default trigger**: queue-depth threshold is read from
 [`PipelineConfig.alert_queue_depth_threshold`](hephaestus/automation/pipeline/coordinator.py)
 (int, non-negative; the CLI tool validates this in `[tool.coverage]`-style
 pre-flight before it ever reaches the coordinator). The constructor
 fails fast on a negative input.
- **Default value**: 100. Operators tune via `--alert-queue-depth-threshold N`
 on `hephaestus-automation-loop`.
- **Resolution events**: an alert transitions to `resolved` when the depth
 drops below the threshold for a tick; `AlertTracker` is responsible
 for emitting the resolved event (the coordinator records it in the
 event log).
- **Failure-mode safety**: alerts are emitted only from measured queue
 depths and circuit-breaker snapshots, never from worker pool internal
 liveness (so a slow worker never causes an alert).

Queue depth measures ready backlog, not a queue fault: a full stage queue is
normal backpressure while a lease, timer, or source cursor retains ownership.
Completion saturation is instead an invariant-breaking fatal error and is not
represented as ordinary queue-depth pressure.

### Health endpoint

[`_health_snapshot`](hephaestus/automation/pipeline/coordinator.py)
serves the JSON shape:

```json
{
 "queue_depths": {"repo": 0, "planning": 3,...},
 "inflight_per_repo": {"Hephaestus": 2},
 "inflight_jobs": 2,
 "circuit_breakers": {},
 "loops_run": 1,
 "status": "ok" | "stopping"
}
```

The `status` field flips to `"stopping"` the moment `shutdown.is_set()`
returns True, so a scratch `/healthz` probe is sufficient for liveness
without subscribing to the event log.

### Dry-run operator check

The canonical operator check is
`hephaestus-automation-loop --dry-run --loops 1 -v`. Stage accessors
log-and-skip mutators; when a stage requests a job the coordinator
logs `[dry-run] would <descr>` and ADVANCEs the item instead of
submitting ([`_run_item`](hephaestus/automation/pipeline/coordinator.py)).
`--dry-run --loops 1` validates seed classification and route
reconstruction end-to-end without consuming rate budget.
Dry-run also overrides two retry semantics that would otherwise stall:

- **RETRY with `retry_delay_s`**: under dry-run a delayed retry waits on
 real-world progress (CI runs, PR merges) the preview will never make,
 so the item finishes with reason `[dry-run] would wait {delay}s`
 ([`_route_retry`](hephaestus/automation/pipeline/coordinator.py)).
- **FAIL_BACK**: a dry-run mutator never writes the gate labels the
 earlier stage would re-check, so a regression would ping-pong until
 the safety cap; dry-run finishes with reason `[dry-run] would
 fail_back` instead ([`_route_fail_back`](hephaestus/automation/pipeline/coordinator.py)).

---

## Legacy compatibility inventory and retirement gates

Compatibility paths accept durable GitHub state written before the current
head-bound review workflow. They remain contained until their observable
retirement conditions are satisfied.

| Compatibility path | Retirement gate |
|---|---|
| `legacy_issue_impl_go_fallback` | After #2055 is deployed, a complete supported-repository seed pass reports zero fallback observations. |
| `already_implementation_go_pr` and `not_implementation_go` | After reviewed-head proof is present for every eligible current-process PR and supported repositories contain zero open legacy implementation-GO PRs. |

---

## 12. Interrupt semantics and exit codes

The coordinator handles SIGINT, SIGTERM, SIGHUP as a two-step shutdown. The
first signal stops new admissions and drains active work into a resumable
state. A second signal, or expiry of the grace window, stops immediately.
Interrupted items record `resumable at <stage>` so durable GitHub state can
reconstruct their queue position on the next run.

Exit-code priority is:

| Priority | Code | Meaning |
|---|---:|---|
| 1 | `130` | SIGINT, SIGTERM, or SIGHUP interrupted the run; this takes priority over other outcomes. |
| 2 | `1` | At least one effective item failed, skipped, or blocked. |
| 3 | `0` | Every effective item completed successfully. |

---

## 13. Glossary

- **Coordinator** — pip line's main thread; owns all queues, the
 timer heap, in-flight registry, routing, signal handlers, GitHub
 APIs. See [`coordinator.py`](hephaestus/automation/pipeline/coordinator.py).
- **Worker pool** — the executor for agent / build/test / git jobs.
 [`WorkerPool`](hephaestus/automation/pipeline/worker_pool.py).
- **WorkItem** — single in-memory record moving through the pipeline.
 [`work_item.py`](hephaestus/automation/pipeline/work_item.py).
- **StageQueue** — FIFO queue for one
 [`StageName`](hephaestus/automation/pipeline/routing.py), owned only
 by the coordinator. [`queues.py`](hephaestus/automation/pipeline/queues.py).
- **CompletionQueue** — the bounded cross-thread payload channel
 (`queue.Queue[(JobHandle, JobResult)]`, capacity `C`). Event latches carry
 wake and saturation signals without queue payloads.
 [`queues.py`](hephaestus/automation/pipeline/queues.py).
- **Durable journal** — GitHub labels, comments, PR state, and
 `ArmingStateStore` records. Restart reconstruction reads this;
 nothing else.
- **Timer-park** — non-blocking retry/backoff by pushing an item onto
 the coordinator timer heap
 ([`_timer_park`](hephaestus/automation/pipeline/coordinator.py)).
- **Resumable** — interrupt outcome. `ItemResult.passed = False`,
 `reason = "resumable at <stage>"`.
- **At-or-past** — label-rank comparison that allows an item to
 short-circuit through earlier stages when it carries a later-stage
 label. Never equality.
- **Head-bound** — an artifact or check whose correctness depends on
 matching the live `headRefOid` of the PR. `pr_review` creates its
 process-local proof only after a GitHub snapshot and a clean checkout agree
 on that SHA; it rechecks the proof before writing the GO label. `merge_wait`
 compares that proof with the confirmed-unarmed live PR and issues a normal
 SHA-conditional merge rather than arming or polling auto-merge.
- **Skip-reason marker** — the `<!-- hephaestus-state-skip-reason -->` HTML-comment marker ([`SKIP_REASON_MARKER`](hephaestus/automation/state_labels.py)) that prefixes every `state:skip` reason-comment body produced by [`format_skip_reason_comment`](hephaestus/automation/state_labels.py), so a repo reader can deterministically trace the automated skip reason.
- **File-system loader** — the Jinja `FileSystemLoader` resolved from `__file__`-relative paths in [`prompts/catalog.py`](hephaestus/prompts/catalog.py); deliberately NOT `PackageLoader` to avoid importlib editable-install staleness (#2308).
- **Conflict-resolution request** — the [`ConflictResolutionRequest`](hephaestus/automation/_review_conflict_resolver.py) immutable context consumed by the cohesive [`ReviewConflictResolver`](hephaestus/automation/_review_conflict_resolver.py) unit split out of `_review_phase.py` (#2209).
- **Advise-skipped breadcrumb** — the [`advise_skipped(reason)`](hephaestus/automation/advise_runner.py) marker string returned by [`run_advise`](hephaestus/automation/advise_runner.py) when Mnemosyne is unavailable, so a stage aborts as `SKIP` rather than failing; the reason is forwarded verbatim from [`resolve_marketplace`](hephaestus/automation/advise_runner.py) (e.g. `clone_failed`, `manifest_missing`).
- **Tool scope** — the explicit `(allowed_tools, permission_mode)` pair in [`AGENT_TOOL_SCOPES`](hephaestus/automation/pipeline/tool_scopes.py) for one of the 9 pipeline agent roles (advise, planner, plan-reviewer, implementer, pr-reviewer, comment-classifier, address-review, ci-driver, learnings); unmapped roles fall through to the read-only [`DEFAULT_TOOL_SCOPE`](hephaestus/automation/pipeline/tool_scopes.py) per the fail-closed security contract (#2319).
- **Reasoning effort** — explicit Codex-only `--<role>-reasoning-effort` CLI flag value (`default|low|medium|high|xhigh`) mapped onto Codex's `model_reasoning_effort`; `default` omits the setting, `low|medium|high|xhigh` override per-role, and omitted flags preserve the model-alias default (#2287).
- **Review posture** — the falsification-first rubric prefix [`REVIEW POSTURE`](hephaestus/prompts/templates/default/review_rubrics/reviewer.j2); combined with anti-inflation grading rules, the max grade is `C` for any dimension the reviewer did not actively attempt to falsify (#2302).
- **Push retry** — [`_git_retry(item, "commit_push failed")`](hephaestus/automation/pipeline/stages/implementation.py) re-attempts a transient push before PR_CREATE; the retry is budget-untouched so the next `implement` attempt remains available (#2274).

- **Severity-aware GO gate** — logic that classifies posted review
 comments by marker (`critical|major|minor|nitpick`) and decides
 whether the `pr_review` round can advance. **See [§5.5 _Gate
 logic_](#55-pr_review) for the authoritative definition and routing
 matrix.**

---
