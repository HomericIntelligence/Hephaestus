# ADR-0066: Plan decomposition into single-PR epics

## Context

The plan-review stage loses information when an implementation plan grows past
`MAX_CURRENT_REVISION_CONTEXT_CHARS` (12,000 characters). An amendment renders
the plan through a bounded excerpt, so a large plan can lose decisions that
earlier reviews accepted. Later reviews then find the same defects again, and
review cycles stop converging. The stage budget permits three reviews per plan
cycle and two cycles, so an issue can take six NOGO verdicts before it stops.

The issue body and its plan comments are the durable requirements record. A
change to the issue body is a change to the authoritative requirements. A new
plan revision, a new plan cycle, or a process restart is not.

## Decision

Add a durable `plan_decomposition` transition inside the plan-review stage.
The stage converts one stalled plan into an epic of single-pull-request
issues. The pipeline topology keeps eight stages.

- **Requirements epoch.** The SHA-256 digest of the authoritative issue body
  (`issue_body_digest`) is the epoch identity. NOGO records bind to one epoch.
  Only an issue-body change starts a new epoch; plan cycles and restarts never
  reset the count.
- **Durable NOGO journal.** Every real NOGO verdict publishes one actor-owned
  comment record bound to the review identity (revision, cycle, round, and
  review text digest). The record publish is idempotent: an exact duplicate
  recovers, a conflicting payload fails closed. Missing verdicts, provider
  errors, timeouts, and BLOCKED outcomes never persist a record.
- **Triggers.** Decomposition starts when either trigger holds after a valid
  NOGO:
  1. *Lossless size* — the complete normalized plan cannot fit the 12,000
     character amendment budget, and at least one real NOGO is confirmed for
     the epoch. A first review can still approve a large plan.
  2. *Convergence* — five confirmed real NOGO records exist for the epoch.
- **Trigger routing.** Until the host can produce and admit one validated
  complete tree, the trigger routes the item to the operator-owned
  `state:plan-blocked` path. The NOGO record publishes first, so the confirmed
  count survives the block.
- **Work-unit domain.** `hephaestus.automation.plan_decomposition` owns the
  closed work-unit schema, safe paths, a closed verification-command grammar,
  requirement and finding coverage checks, duplicate-scope checks, and an
  iterative Kahn topological sort. No configured child-count or tree-depth
  limit exists; text and payload limits are the single-PR leaf condition.
- **Record protocol.** Intent, draft manifests, review receipts, seals,
  bindings, summaries, and transition receipts are actor-owned comments with
  role markers and canonical JSON payloads. Recovery re-reads GitHub and
  matches the role marker. A foreign marker is kept, and the host writes no
  duplicate.
- **Transaction.** After a trigger holds, the plan-review stage runs one
  durable transaction before any mutation: a one-shot generation session
  produces a complete normalized draft manifest; a separate one-shot
  independent review session, bound to the complete draft digest, must return
  a `GO` verdict before the host seals the tree. The host then creates or
  recovers every child issue by its deterministic marker, verifies one exact
  readback per child against the seal, adds the confirmed child labels,
  transitions the parent to the exclusive epic state with a summary comment,
  and only then publishes the child bindings and finishes the parent with the
  complete child manifest.
- **Same-run recursion rule.** A generated child issue never decomposes in
  the same run. When a work item's issue number is in the coordinator-owned
  current-run generated set, the stage persists its NOGO record, confirms
  `state:plan-no-go`, creates no decomposition artifacts, and defers the item
  to a later run.
- **Fail-closed recovery.** Restart reconciliation resumes at the
  independent-review step and fast-forwards through recovered records. A
  corrupt draft blocks the item as `plan_decomposition_draft_unreadable`.
  Provider failures and GitHub write failures retry within the bounded
  budget and then block the item; no partial tree is admitted. An ambiguous
  child create or readback fails closed, and the stage never guesses a
  binding.

## Alternatives considered

- **Raise the context and iteration limits.** This delays failure, does not
  repair the lossy amendment, and permits more review oscillation.
- **Always apply `state:plan-blocked`.** Safe, but it forces an operator to
  split work the plan already describes. The blocked path stays as the
  fail-closed fallback for unsafe or incomplete decomposition trees.
- **Fix the child count.** A fixed count does not follow the technical
  boundaries of the work; it can combine unrelated changes or create
  artificial fragments.

## Consequences

- The convergence limit is durable: the fifth real NOGO cannot silently start
  a sixth review after a restart, because the count reads GitHub records.
- The lossless-size limit protects every accepted decision: a NOGO on an
  oversized plan never amends from a truncated excerpt.
- The plan-review stage owns one new pure dependency
  (`hephaestus.automation.plan_decomposition`) and one record protocol in
  `hephaestus.automation.pipeline.plan_journal`.
- Model output stays untrusted: only the host validates trees, publishes
  records, creates issues, writes labels, and queues work.
- The coordinator owns child admission: it re-reads the durable records,
  verifies each child issue against the sealed specification and its
  confirmed labels, and queues ready children one topological layer at a
  time. Dry-run publishes no decomposition mutation and queues no child.
