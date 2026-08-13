# ADR-0026: Auxiliary host-learning lane

- Status: Accepted
- Date: 2026-08-11
- Tracks: #2705

## Context

Approved-plan and post-merge learning ran on the main pipeline worker pool.
One slow host operation could stop unrelated work when the main pool had one
worker. Cleanup also had to remain after learning, so moving only the host call
was not sufficient.

## Decision

Use an implicit `learning → finished` auxiliary lane. Plan review and merge
wait emit immutable, deterministic learning intents. They do not submit a
learning job. `LearningStage` owns a lock-guarded journal with `pending`,
`claimed`, `succeeded`, and `failed` states. Only a committed claim can submit
one host-owned `AthenaSkillJob`.

Use an independent bounded pool and permit budget. The pool accepts host
learning and the two terminal cleanup operations only. It has no generic agent
dispatch. Destination capacity is reserved before source capacity is released.
Learning failure is ancillary. Cleanup starts only after all intents are
terminal.

For terminal main work, create a compact post-processing record before the
handoff. Keep only the confirmed result, intent keys, resume stage, and
cleanup receipts. Do not keep PR diffs, review audits, prompts, or agent
output in the auxiliary backlog.

The rollback gate is strict. Do not run an older executable while a new
learning-journal intent is nonterminal. Drain or park the new lane and handle
all nonterminal intents with the new version first. An older executable cannot
read the new journal and can repeat host delivery.

## Alternatives considered

- Keep learning on the main pool. Rejected because it blocks unrelated work.
- Use an unbounded spill backlog. Rejected because it removes backpressure.
- Dispatch generic agent jobs from the auxiliary pool. Rejected because
  Athena learning is a host boundary, not a provider lane.
- Dual-write the legacy record. Rejected because it adds a migration platform
  without a product requirement.

## Consequences

Main work can continue while learning runs. Learning and cleanup have separate
capacity, completion, shutdown, and failure accounting. A crash-left claimed
intent fails as `outcome_unknown` instead of risking a duplicate delivery.
Operators must observe the rollback gate until all new journal records are
terminal.

This decision provides scheduling, recovery, and cleanup infrastructure. It
does not prepare Mnemosyne content or the host-owned delivery request. Issue
2754 owns that production capability. Until it is complete, a request without
a prepared delivery fails as ancillary post-processing. The primary pipeline
result does not change, and cleanup still waits for that terminal failure.
