# ADR-0018: Reviewer-owned PR review-thread reconciliation

- Status: Accepted
- Date: 2026-07-28
- Tracks: #2511
- Supersedes: Author- and receipt-scoped review-thread handoff behavior

## Context

An open pull-request review thread is durable review work regardless of who
opened it. Earlier automation paths classified threads by author or a
process-specific receipt and could leave a different class for a human to
resolve. That both loses actionable feedback and creates a second, incomplete
thread lifecycle outside the queue pipeline.

The implementation and reviewer agents have distinct responsibilities. The
implementation agent can investigate and change code, but cannot independently
prove that its change and explanation satisfy the review concern. The reviewer
can validate that evidence, but must not close a thread based only on model
prose or a stale PR snapshot.

## Decision

1. Every open review thread enters implementation remediation without regard
   to author, severity marker, or historical receipt ownership. The
   implementation agent investigates each actionable thread, fixes it, and
   returns a concise reply describing the change. It never resolves a thread.
2. The host posts an implementation reply only after a real fix commit is
   pushed. Before and after each reply it requires a complete thread snapshot
   and an open, unarmed PR at the exact expected head. A later pass may recover
   a candidate reply from the final, viewer-owned thread comment on GitHub only
   when its marker recomputes for that exact thread and head; fresh GitHub
   state, not process-local memory, remains the authority for reconciliation.
3. The reviewer reads every open thread. It may decide only the current
   implementation-reply receipts: it resolves a validated fix, or posts
   precise corrective feedback and leaves the thread open. The adapter repeats
   the complete-snapshot and exact-head checks immediately before each
   mutation.
4. The former standalone review-loop facade is retired. It cannot offer an
   alternate reply, validation, or resolution path.

## Alternatives considered

- **Let implementation agents resolve their own threads.** Rejected: it
  removes independent verification of both the code change and its explanation.
- **Hand non-automation threads to a human.** Rejected: author is not a valid
  proxy for whether feedback is actionable, and it strands review work.
- **Trust a persisted or process-local receipt after restart without a fresh
  GitHub check.** Rejected: cached evidence cannot prove the current thread or
  head state. A marker-backed candidate recovered from GitHub is admissible
  only after the complete exact-head snapshot checks in this decision.

## Consequences

- A thread may need multiple implementation/reviewer turns, but every
  rejection has durable feedback and remains visible to the next implementer.
- A restart or concurrent PR update fails closed: no stale reply receipt may
  resolve a current GitHub thread.
- The queue can apply implementation approval only after the fresh open-thread
  read is empty; review prose and CI remain non-authoritative for that label.
