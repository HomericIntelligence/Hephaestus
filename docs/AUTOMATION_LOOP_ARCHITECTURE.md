# Automation Loop Architecture

Status: implemented for the epic #1809 queue-based automation loop. The
`hephaestus-automation-loop` CLI runs this pipeline directly; the legacy
subprocess-per-phase loop was removed after the #1818/#1819 cutover.

## Overview and goals

The automation loop is a single-coordinator, eight-queue state-machine
pipeline. The coordinator (main thread) owns queues and performs validation,
logging, and GitHub manipulation. A single worker pool executes all agent
invocations, build/test subprocesses, and git/network operations. GitHub labels
and PR state are the persistent journal; queues are in-memory and reconstructed
from labels at startup. An interrupt leaves items resumable, never failed.

## Queue topology

### Mermaid

```mermaid
flowchart LR
  repo --> planning --> plan_review --> implementation --> pr_review --> ci --> merge_wait --> finished
  plan_review -- "NOGO (plan_review_iter 3 / plan_cycles 2)" --> planning
  implementation -- "agent err" --> implementation
  pr_review -- "agent_error" --> implementation
  ci -- "fix (in-stage)" --> ci
  ci -- "fix_exhausted" --> implementation
  merge_wait -- "FAILING → ci_red" --> ci
  merge_wait -- "DIRTY → rebase (in-stage)" --> merge_wait
  merge_wait -- "BLOCKED → blocked_exhausted" --> pr_review
```

### ASCII

```
repo ─> planning ─> plan_review ─> implementation ─> pr_review ─> ci ─> merge_wait ─> finished
             ^             │              ^   ^           │  ^      │  ^       │
             └─── NOGO ────┘              │   └ agent err ┘  └ fix ─┘  └ DIRTY→rebase (in-stage),
                (iter 3, cycles 2)        └── fix_exhausted             FAILING→ci, BLOCKED→pr_review
```

The diagrams show the primary flow and the most common regressions only. The
complete edge set — including implementation → plan_review (`plan_not_go`),
implementation → ci (`already_implementation_go_pr`), and ci → pr_review
(`not_implementation_go`) — is normative in the ROUTES table below.

## Coordinator / worker contract

The main thread (coordinator) owns all eight in-memory stage queues and
performs ONLY: arg parsing, queue seeding, queue draining, validation/logging,
and GitHub API mutations (labels, comments, PR create/merge-arm — sub-second
calls). It never launches agent workflows, build/test subprocesses, or
git-network operations.

A single worker pool executes:

- **Agent jobs**: call prompt-builder callables (which may fetch diffs/bodies
  via `gh`), then invoke an agent runtime, with optional result parsing
  (e.g., `parse_review_verdict`).
- **Build/test jobs**: execute subprocess commands in worktrees (e.g., `pixi
  run pytest`).
- **Git jobs**: clone, worktree management, rebase, push — all git/network
  operations (protected by per-repo `threading.Lock` since worktrees share
  `.git`).

The only cross-thread channel is `CompletionQueue = queue.Queue[(JobHandle,
JobResult)]`, whose blocking `get(timeout=…)` also serves as the loop's idle
sleep. When a worker starts executing a submitted job it logs a `worker_claim`
line with the stable worker thread ID plus the coordinator item/stage claim
context. The returned `JobResult` carries that same `worker_id`, and the
coordinator persists it in the durable `complete` event record so operators can
correlate queue drain/submission with actual worker execution.

## WorkItem lifecycle

In-memory per-stage mini-states (stage-local, never as labels) vs. the small
(~6-label) GitHub `state:*` vocabulary (from `state_labels.py`):
`state:needs-plan`, `state:plan-no-go`, `state:plan-go`,
`state:implementation-no-go`, `state:implementation-go`, `state:skip`.

WorkItem `state` field is in-memory ONLY and reconstructed from GitHub labels
at startup. Labels stay durable and small. Every per-stage state-machine
mutation must be journaled as a durable GitHub write (label, comment, PR
create) BEFORE the corresponding queue push — so restart = re-run, and
interrupts leave items RESUMABLE, never FAILED.

## Stages

Legend: **[M]** = coordinator main thread; **[W:A]** = worker Agent job;
**[W:B]** = worker BuildTest job; **[W:G]** = worker Git job. Every durable
write (label apply, comment post, PR create) happens BEFORE the outcome that
causes a queue push.

### 1. repo (kind=REPO)

**States**: ENTER → CLONE_WAIT → DISCOVER → SEEDED.

**Steps**:

1. [M] `ensure_state_labels` — initialize labels on all repos.
2. [W:G] Clone missing repos (parallel across worker pool).
3. [M] List issues, dedup, partition epics → tag epics `state:skip` [durable],
   exclude them, run label-based classifier to assign entry queues, build
   dependency graph.
4. [M] Discover and fast-forward: `--drive-green-all` → orphan PRs (PRs with no
   tracked issue) → ci stage.
5. [M] Push repo's discovered issues to their classified entry queues; advance
   repo item → finished (pass, seeded: N issues).

**Verdicts**: terminal — the repo item itself always advances to finished
(pass, seeded: N) once seeding completes; clone exhaustion → finished(fail).

**Budgets**: `clone` = 2 (max clone attempts per repo).

**Owned labels**: none (epics receive `state:skip` before exclusion).

**Prompt functions**: none.

### 2. planning

**States**: ENTER → ADVISE_WAIT → PLAN_WAIT → VERIFY.

**Steps**:

1. [M] on_enter: fast-forward check (if at-or-past `state:plan-go` →
   ADVANCE; if `state:skip` → SKIP).
2. [W:A] **Advise step** — `prompts/advise.py:130 get_advise_prompt_builder`.
3. [W:A] **Plan step** — `prompts/planning.py:232 get_plan_prompt` (session:
   repo, issue, planner model; plan comment = durable artifact).
4. [M] Verify plan comment exists (check `PlannerStateManager`) → ADVANCE or
   RETRY.

**Verdicts**: ADVANCE, RETRY, FAIL_BACK(reason).

**Fail routes**: default = finished(fail).

**Budget**: `plan` = 2 (max plan attempts per issue).

**Owned labels**: `state:needs-plan` (idempotent, on entry) [durable].

**Prompt functions**:

- `prompts/advise.py:130 get_advise_prompt_builder`
- `prompts/planning.py:232 get_plan_prompt`

### 3. plan_review

**States**: ENTER → REVIEW_WAIT → EVAL → AMEND_WAIT → (loop) → LEARN_WAIT.

**Steps**:

1. [W:A] **Review step** — `prompts/planning.py:270 get_plan_loop_review_prompt`;
   verdict parsed in-worker by `claude_invoke.parse_review_verdict` (GO,
   NOGO, AMBIGUOUS, ERROR).
2. [M] **EVAL**: if GO → apply `state:plan-go` label [durable] → ADVANCE; if
   NOGO and iteration < 3 → proceed to step 3; if NOGO/AMBIGUOUS at the
   iteration cap → apply `state:plan-no-go` label [durable], then
   FAIL_BACK(nogo) while plan_cycles remain or
   FAIL_BACK(plan_cycles_exhausted) once plan_cycles is exhausted; if ERROR →
   leave labels untouched, RETRY next tick.
3. [W:A] **Amend step** — resume planner session with feedback block.
   [M] Upsert the amended plan comment [durable] before looping back to
   review. The iteration counter increments in EVAL when each real review
   verdict is processed.
4. [W:A] **Learn step** (on GO only) — `learn.py:111 build_learn_prompt`.

**Verdicts**: ADVANCE, RETRY, FAIL_BACK(nogo, plan_cycles_exhausted).

**Fail routes**: default = planning (previous queue); `plan_cycles_exhausted`
→ finished(fail).

**Budgets**: `plan_review_iter` = 3 (max review iterations), `plan_cycles` = 2
(max plan→review→amend cycles before giving up).

**Owned labels**: `state:plan-go` (GO verdict) [durable], `state:plan-no-go`
(exhausted) [durable].

**Prompt functions**:

- `prompts/planning.py:270 get_plan_loop_review_prompt`
- `learn.py:111 build_learn_prompt`

### 4. implementation

**States**: ENTER → GATE → WORKTREE_WAIT → DIRTY_DECISION_WAIT →
ADVISE_WAIT → IMPLEMENT_WAIT → TEST_WAIT → TESTFIX_WAIT → COMMIT_PUSH_WAIT →
PR_CREATE.

**Admission**: dependency topological order + file-overlap serialization +
per-repo in-flight cap.

**Steps**:

1. [M] **GATE**: verify `is_plan_review_go` (at-or-past); detect existing-PR
   fast path (per `_review_existing_pr` semantics) → skip to step 8.
2. [W:G] Create/refresh worktree (`worktree_manager.create_worktree(
   refresh_base=True)`).
3. [W:A] **Dirty worktree decision** — `prompts/implementation.py:299
   get_dirty_reused_worktree_decision_prompt`.
4. [W:A] **Advise step**.
5. [W:A] **Implement step** — `prompts/implementation.py:217
   get_implementation_prompt`.
6. [W:B] **Test step** (optional) — `_run_tests_in_worktree` (`pixi run
   pytest`); on failure, RETRY with budget test_fix.
7. [W:A] **Test fix step** (on test failure, budget test_fix = 1) — resume
   with test-failure feedback → repeat step 6.
8. [W:G] Commit and push (or no-op if existing-PR).
9. [M] **PR_CREATE**: call `gh pr create` (idempotent for existing) with
   `prompts/pr_review.py:352 get_pr_description` [durable] → ADVANCE.

**Verdicts**: ADVANCE, RETRY, FAIL_BACK(reason).

**Fail routes**: `plan_not_go` → plan_review; `already_implementation_go_pr`
(existing PR detected) → ci (declared route, skips pr_review); `agent_error`
→ RETRY (consumes the `implement` budget); exhaustion → finished(fail).

**Budgets**: `implement` = 2 (bounds implement-step attempts, including
`agent_error` retries), `test_fix` = 1 (retry on test failure).

**Owned labels**: PR creation is the journal entry (no labels needed).

**Prompt functions**:

- `prompts/implementation.py:299 get_dirty_reused_worktree_decision_prompt`
- `prompts/implementation.py:217 get_implementation_prompt`
- `prompts/pr_review.py:352 get_pr_description`

### 5. pr_review

**States**: ENTER → REVIEW_WAIT → VALIDATE_WAIT → POST → DIFFICULTY_WAIT →
ADDRESS_WAIT → PUSH_WAIT → EVAL → (loop) → FOLLOWUP_WAIT.

**Steps**:

1. [W:A] **Inline review step** — `prompts/pr_review.py:104
   get_pr_review_analysis_prompt` via `pr_reviewer.review_pr_inline`; output
   is review body.
2. [W:A] **Validation step** — `prompts/pr_review.py:232
   get_review_validation_prompt`.
3. [M] **POST**: post surviving review threads and comments to PR [durable].
4. [W:A] **Difficulty step** — `prompts/pr_review.py:310
   get_comment_difficulty_prompt`.
5. [W:A] **Address step**: if fresh PR → resume implementer with
   `prompts/implementation.py:342 get_impl_resume_feedback_prompt`; if
   existing-PR path → `prompts/address_review.py:181
   get_address_review_prompt`.
6. [W:G] Push (commit+force-push addressing changes).
7. [M] **EVAL**: invoke `_evaluate_go_verdict` (parse reviewerAgent verdict:
   GO, NOGO, AMBIGUOUS, ERROR, HUMAN_BLOCKED) + count unresolved threads;
   an explicit NOGO with zero posted thread IDs and zero unresolved automation
   or human threads is not a completed round: upsert the bounded
   `<!-- hephaestus-pr-review-zero-thread-nogo -->` artifact, emit the typed
   `pr_review_zero_thread_nogo` event, and re-enter `REVIEW_WAIT` for a fresh
   reviewer invocation without consuming a round;
   if GO + 0 unresolved threads → apply `state:implementation-go` label and
   arm auto-merge [durable] → ADVANCE; if NOGO/AMBIGUOUS/ERROR and iteration
   < 3 → RETRY; if HUMAN_BLOCKED or iteration cap exhausted → routes depend on
   iteration (hard cap 6) and unresolved-thread progress; on exhaustion →
   apply `state:skip` label [durable] → SKIP.
8. [W:A] **Follow-up step** (on GO only) — `prompts/follow_up.py:105
   get_follow_up_prompt`.

**Verdicts**: ADVANCE, RETRY, SKIP, BLOCKED (human intervention needed),
FAIL_BACK(reason).

**Fail routes**: `agent_error` → implementation (retry from implement);
`exhaustion` → SKIP (apply state:skip label); `human_blocked` →
finished(fail, human_blocked); default → pr_review (RETRY).

**Budgets**: `pr_review_iter` = 3 (soft; max iterations while threads
decrease), `pr_review_hard` = 6 (hard cap; iterations 4-6 only if
unresolved-thread count decreases).

Zero-thread NOGO anomalies use the bounded reviewer-error retry cap and
consume neither `pr_review_iter` nor `pr_review_hard`; cap exhaustion
escalates directly with `state:skip` (never `agent_error`) and does not
write `state:implementation-no-go`. A threadless NOGO can be a deliberate,
deterministic reviewer verdict (prose-only, no line-anchored findings) —
failing back through `agent_error` would re-adopt the same PR through
implementation with nothing new to address and re-review cannot change a
deterministic input, so cap exhaustion stands down instead of ping-ponging
to a dead end (#2079). Stage-originated JSONL events use the closed schema in
`pipeline/events.py`; raw reviewer text, GitHub bodies, and arbitrary event
objects are rejected.

**Owned labels**: `state:implementation-go` (GO verdict) [durable],
`state:implementation-no-go` (NOGO verdict, before retry/regress) [durable],
`state:skip` (exhaustion) [durable].

**Prompt functions**:

- `prompts/pr_review.py:104 get_pr_review_analysis_prompt`
- `prompts/pr_review.py:232 get_review_validation_prompt`
- `prompts/pr_review.py:310 get_comment_difficulty_prompt`
- `prompts/implementation.py:342 get_impl_resume_feedback_prompt`
- `prompts/address_review.py:181 get_address_review_prompt`
- `prompts/follow_up.py:105 get_follow_up_prompt`

### 6. ci

**States**: ENTER → DISCOVER → REBASE_WAIT → POLL → FIX_WAIT → PUSH_WAIT →
(POLL).

**Steps**:

1. [M] **DISCOVER**: fetch PR state via `pr_discovery`; verify
   `is_implementation_go` (fast-forward if not).
2. [W:G] **Mechanical rebase** (optional, if base changed): attempt rebase via
   git; on success, push.
3. [M] **POLL** (non-blocking): call
   `ci_run_coordinator.classify_ci_state(ctx.github.pr_checks(pr))`, the
   shipped pure classifier imported by `hephaestus.automation.pipeline.stages.ci`
   and covered by
   `tests/unit/automation/pipeline/stages/test_classify_ci_state.py`. It returns
   PENDING, GREEN, FAILING, or terminal states. If PENDING → RETRY with timer
   backoff; if GREEN → ADVANCE; if FAILING → step 4.
4. [W:A] **CI fix step** (budget ci_fix = 1) — `ci_fix_orchestrator.py:498
   build_ci_fix_prompt`; escalation via `ci_fix_orchestrator.py:148
   force_engagement_prompt`.
5. [W:G] Push fix commit(s).
6. Loop back to step 3 (POLL).

**Verdicts**: ADVANCE (GREEN), RETRY (PENDING, fix needed), FAIL_BACK(reason).

**Fail routes**: `fix_exhausted` → implementation (retry from implement);
`not_implementation_go` → pr_review (regress); `no_pr` → finished(fail);
default = ci (RETRY).

**Budgets**: `ci_fix` = 1 (max fix attempts; one escalation via
force_engagement), `rebase` = 2 (max mechanical rebase attempts).

**Owned labels**: none (ci state is reflected in PR check conclusion).

**Prompt functions**:

- `ci_fix_orchestrator.py:498 build_ci_fix_prompt`
- `ci_fix_orchestrator.py:148 force_engagement_prompt`

### 7. merge_wait

**States**: ENTER → ARM → POLL → DIRTY_REBASE_WAIT/BLOCKED_ADDRESS_WAIT →
(POLL) → LEARN_WAIT.

**Steps**:

1. [M] **ARM**: ensure auto-merge is armed (via `pr_manager.mark_pr_*` and
   `arming_state`) [durable]; if already armed, idempotent.
2. [M] **POLL** (non-blocking): fetch PR state → MERGED, CLOSED, FAILING,
   DIRTY, BLOCKED, or PENDING; if PENDING → RETRY with timer backoff.
3. On MERGED → step 4; on FAILING → FAIL_BACK(ci_red); on DIRTY → step 5a; on
   BLOCKED → step 5b; on CLOSED → finished(fail).
4. [W:A] **Post-merge learn** (deduped via `arming_state`) — learn prompt.
5a. [W:G]+[W:A] **Resolve dirty PR** — mechanical rebase + push (budget
   rebase = 2); loop back to POLL.
5b. [W:A] **Address blocked threads** (budget blocked_address = 2) —
   `get_address_review_prompt`; [W:G] push → loop back to POLL.

**Verdicts**: ADVANCE (MERGED), RETRY (PENDING, DIRTY, BLOCKED in-stage),
FAIL_BACK(reason).

**Fail routes**: `ci_red` → ci (regress); `blocked_exhausted` → pr_review
(regress); `timeout` → finished(fail); `closed` → finished(fail).

**Budgets**: `blocked_address` = 2 (max address attempts for blocked threads),
`rebase` = 2 (max mechanical rebase attempts), `merge` = --max-merge-attempts
(total merge-attempt timeout, not touched by pipeline).

**Owned labels**: none (merge state is PR state).

**Prompt functions**:

- `prompts/address_review.py:181 get_address_review_prompt`
- `learn.py:111 build_learn_prompt` (post-merge deduped)

### 8. finished

**States**: ENTER → RECORD → CLEANUP → DONE.

**Steps**:

1. [M] Record `ItemResult` in run ledger.
2. [W:G] Worktree cleanup: remove on pass, preserve on fail for debugging
   (preserved list in end-of-run summary).

**Verdicts**: terminal (no outgoing routes).

**Owned labels**: none (result is recorded in summary).

**Prompt functions**: none.

## ROUTES table

Failure routing (single declarative location; per-stage fail-target and
budgets). All budgets are per-item-lifetime counters stored in
`WorkItem.attempts`; they are NEVER reset when an item re-enters a stage, so
cross-stage regression cycles (e.g. merge_wait → ci → implementation →
pr_review → ci) remain globally bounded.

| Stage | Next (success) | Fail targets | Budgets |
|-------|---|---|---|
| repo | finished(pass) — repo item is terminal; discovered issues/PRs go to their classified entry queues | finished(fail) on clone exhaustion | clone=2 |
| planning | plan_review | finished(fail) | plan=2 |
| plan_review | implementation | planning (nogo, default), finished(fail) on plan_cycles_exhausted | plan_review_iter=3, plan_cycles=2 |
| implementation | pr_review | plan_review (plan_not_go), ci (already_implementation_go_pr), finished(fail) on exhaustion | implement=2, test_fix=1 |
| pr_review | ci | implementation (agent_error), finished(fail) on human_blocked, finished(skip) on exhaustion | pr_review_iter=3, pr_review_hard=6 |
| ci | merge_wait | implementation (fix_exhausted, missing_worktree), pr_review (not_implementation_go), finished(fail) on no_pr | ci_fix=1, rebase=2 |
| merge_wait | finished(pass) | ci (ci_red), implementation (missing_worktree), pr_review (blocked_exhausted), finished(fail) on closed/timeout | blocked_address=2, rebase=2, merge=--max-merge-attempts |
| finished | — | — | — |

## Seeding and reconstruction

One classifier serves both initial seeding (`--repos`, `--issues`, `--prs`) and
restart reconstruction (at startup, scan GitHub for labels/PR state). Direct PR
inputs are terminalized at the seed boundary when their PR is already merged or
closed: merged PRs become `finished(pass)` and closed PRs become
`finished(fail)`, before branch adoption or label-based routing is attempted.
Open direct PRs enter the target repo's `pr_review` queue unless the PR already
carries `state:implementation-go`, in which case they enter `ci`.

The same terminal-state check is repeated at the CI and implementation stage
boundaries before branch adoption or implementation-label routing. This makes a
PR that closes or merges between seeding and stage execution terminal without
attempting to adopt its branch or run further work.
Uses ordered label rank at-or-past comparisons (never equality):

- `state:needs-plan` — rank 0 (lowest).
- `state:plan-no-go` — rank 1.
- `state:plan-go` — rank 2.
- `state:implementation-no-go` — rank 3.
- `state:implementation-go` — rank 4 (highest).

`state:skip` carries no rank: it is handled by exclusion (a skipped item
never enters the rank comparison at all), matching its absolute exclusion
semantics.

| GitHub state | Entry queue | Notes |
|---|---|---|
| state:skip or epic | excluded | Epic tagging is the one seeding write; done BEFORE excluding. |
| Direct PR already merged | finished | pass, idempotent; terminalized before branch adoption. |
| Direct PR already closed | finished | fail; terminalized before branch adoption. |
| Open PR + PR carries state:implementation-go | ci | existing-PR advanced to merge-ready. |
| Open PR, no impl-go | pr_review | existing-PR path; will be reviewed. |
| No PR, at-or-past state:plan-go | implementation | plan approved; ready to implement. |
| No PR, state:plan-no-go | planning | plan rejected; amend with feedback. |
| state:needs-plan / no label | planning | entry point; no plan yet. |

**Thin pipeline scopes** (within `hephaestus-automation-loop`):

- `--repos` seeds one repo item per named repository.
- `--issues` seeds issue-scoped items through the classifier and routes them to
  planning, implementation, pr_review, ci, or finished according to durable
  labels/PR state. When explicit issue or PR scope is present, the resolved
  repository list is used only as context for those items; repo discovery is not
  enqueued, so a scoped run cannot reconstruct every open issue in the repo.
- `--org` expands to non-fork, non-archived repository seeds.

The standalone console scripts are thin queue-pipeline scoped entry points.
They preserve the historical CLI surfaces while building a `PipelineConfig`
limited to the matching stage slice.

## Interrupt semantics and exit codes

`Coordinator.run()` installs SIGINT, SIGTERM, and SIGHUP handlers unless tests
disable signal installation. The first signal sets the shutdown event and starts
a graceful drain window (`PipelineConfig.grace_s`, default 30s). During that
window the coordinator stops admitting new work, drains completed jobs, and
parks touched items as resumable. A second signal, or an expired grace window,
tears down the worker pool immediately and synthesizes interrupted results for
remaining in-flight jobs.

Interrupted items never route through stage success/failure logic. The
coordinator records them as `resumable at <stage>` and the end-of-run summary
prints them under `=== Pipeline summary ===`; with `--json`, the JSON envelope
also carries a `resumable` list. Queued and timer-parked items are finalized the
same way on shutdown. Resume is therefore label/PR/worktree reconstruction:
rerun the same scoped command and seeding will classify each issue back into
the correct entry queue. There is no persisted queue snapshot.

Summary rows, preserved worktree guidance, and exit-code calculation use the
latest effective logical item for each issue, PR, or repository. When a logical
item is re-seeded, superseded historical attempts are collapsed before these
outputs are produced: an old failed attempt does not create a failure row,
preserved-worktree hint, or non-zero exit code after a later effective attempt
passes. The effective-item rule applies only to superseded attempts; the
current item's own failed, skipped, or blocked result still counts.

Exit codes are stable: `130` for interrupted runs, `1` if any effective item
failed, skipped, blocked, or the coordinator itself hit a fatal error, and `0`
for a clean run. If an interrupt overlaps a non-passing ledger entry or fatal
coordinator error, `130` deliberately takes priority because the run did not
complete.

## Concurrency and tuning

The coordinator thread is the only owner of `WorkItem`, `StageQueue`, timers,
routing, and GitHub mutations. Worker threads receive immutable job requests
and return `(JobHandle, JobResult)` through the completion queue. Pool size is
`parallel_repos * max_workers`; `max_workers` also caps in-flight work per
repo. Implementation admission adds dependency ordering and file-overlap
serialization unless `--no-serialize-file-overlap` is passed.

The pipeline never sleeps inside stage logic. Backoff uses the coordinator's
timer heap, and low GitHub rate budget parks agent jobs until the reset instead
of blocking the loop. `--phase-timeout` bounds each agent job inside the
queue pipeline.

Dry-run mode logs GitHub mutations and job submissions without executing them;
`_submit` asserts that no worker job is submitted in dry-run. This makes
`hephaestus-automation-loop --dry-run --loops 1 -v` the operator check
for seed classification and route reconstruction.

## CLI scopes and rollout controls

`hephaestus-automation-loop` runs the queue pipeline directly; there is no
`--pipeline` compatibility flag, there is no `--legacy-loop` rollback path, and
`HEPH_PIPELINE` no longer selects a subprocess-per-phase implementation.

The default pipeline's scopes are the `hephaestus-automation-loop` selectors
listed above. Standalone scripts are thin queue-pipeline scoped entry points:

- `hephaestus-plan-issues` preserves the historical planner CLI and dispatches
  the planning/plan_review stage slice.
- `hephaestus-implement-issues` preserves the historical implementer CLI and
  dispatches the implementation/pr_review stage slice after the plan-go gate.
- `hephaestus-review-prs` preserves the historical reviewer CLI and dispatches
  the pr_review stage slice.
- `hephaestus-drive-prs-green` preserves the historical drive-green CLI and
  dispatches the ci/merge_wait stage slice.
- `hephaestus-merge-prs` remains a manual merge-driving command outside the
  queue coordinator.

`--run-pre-pr-tests` is an opt-in queue-runner flag that enables the
implementation-stage pre-PR unit-test gate before commit and PR creation. The
stage executes `PipelineConfig.pre_pr_test_argv` as an argv vector; CLI users get
the repository default test command through the boolean flag.

## Glossary

- **Coordinator**: the main-thread event loop that owns queues, routing,
  timers, GitHub writes, summaries, and signal handling.
- **Worker pool**: the executor for agent, build/test, and git jobs. Workers
  never mutate queues directly.
- **WorkItem**: an in-memory repo, issue, or PR unit moving through a stage.
- **StageQueue**: FIFO queue for one `StageName`, owned only by the
  coordinator.
- **CompletionQueue**: the only cross-thread channel from workers back to the
  coordinator.
- **Durable journal**: GitHub labels, comments, PR state, and local worktrees;
  this is what restart reconstruction reads.
- **Timer-park**: non-blocking retry/backoff by moving an item to the
  coordinator timer heap.
- **Resumable**: interrupted item outcome. It is not a failure verdict and is
  reconstructed from durable state on the next run.
