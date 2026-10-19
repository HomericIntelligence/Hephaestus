# ADR-0058: Durable NATS failure delivery and replay

- Status: Accepted
- Date: 2026-10-19
- Tracks: #2395

## Context

The subscriber historically acknowledged decode and handler failures without
retaining them. `docs/nats.md` documented this as a deliberate at-most-once
contract for events that an upstream source can reconstruct. Operated
workflows need durable failed-message storage, an audit trail across
restarts, and operator-driven replay. Withholding acknowledgement alone is not
a substitute: a permanently failing message then creates an unbounded
poison-message redelivery loop, and a crashed worker has no durable record of
what it processed.

## Decision

Adopt durable failure delivery for the JetStream subscriber:

- Four auto-provisioned streams hold the contract:
  `HEPHAESTUS_NATS_FAILURES` (one dead-letter envelope per failure),
  `HEPHAESTUS_NATS_DELIVERY_LEDGER` (one record per event),
  `HEPHAESTUS_NATS_ADVISORIES` (max-delivery advisories for the configured
  bindings), and `HEPHAESTUS_NATS_REPLAY_RECEIPTS` (one receipt per replay
  request). Provisioning creates the streams when absent and validates
  existing limits without mutation.
- One event gets one durable identity from its (stream, consumer, stream
  sequence) triple. The ledger enforces an explicit state machine
  (`ready → running → completed → acked`, with `ready → handed_off` at the
  processing budget). Every write carries the previous record version in a
  `Nats-Expected-Last-Subject-Sequence` header, so concurrent workers
  converge on one owner and a restart resumes the shared budget instead of
  in-memory counters.
- The listener runs the handler inside a heartbeat context that renews the
  JetStream delivery and the ledger lease. The defaults bound every timer:
  `ack_wait_seconds = 30`, `heartbeat_interval_seconds = 10`
  (`<= ack_wait / 3`), `ledger_lease_seconds = 30` (`>= 2x heartbeat`),
  `max_processing_attempts = 3` with a redelivery backoff schedule, and
  broker `max_deliveries = 6` above the processing budget.
- Disposition rules: a success completes, acks, and closes the record; a
  failure below the budget NAKs with backoff; a failure at the budget
  publishes the dead-letter envelope idempotently by identity and TERMs the
  delivery. A payload above the capture bound, or metadata without a durable
  identity, takes no client disposition; the broker redelivers up to
  `max_deliveries` and the max-delivery advisory reconciler converts the
  exact advisory `stream_seq` into a durable `handler_outcome_unknown`
  record.
- Full-size payloads at or under the exact 1 MiB capture bound stay
  base64-retained inside the envelope. The dead-letter stream caps
  `max_msg_size` at 1,414,488 bytes, which holds one exact-bound envelope.
  Oversized payloads keep durable metadata-only records.
- Consumers are explicit `AckPolicy.EXPLICIT` pull consumers. An
  incompatible pre-existing durable fails startup closed by default.
  Migration is opt-in through `migrate_incompatible_consumers`; it
  snapshots, deletes, and recreates the consumer only while it is unbound,
  resumes at `ack_floor + 1`, and restores the snapshot when recreation
  fails.
- Replay is idempotent per request id: `FailureReplayer.replay(failure_id,
  request_id)` republishes the retained payload to the source subject with
  deterministic replay headers and `Nats-Msg-Id` broker deduplication. The
  receipt CAS transitions `publishing` and then `completed`; a restart that
  repeats the same request id returns the original publish acknowledgement
  without a second publication. A stuck `publishing` receipt reports
  `UNKNOWN` rather than risking a duplicate.

## Consequences

- Broker sizing changes: `max_payload` must exceed 1,414,488 bytes, and the
  JetStream store limit must exceed the declared reservations (about
  1.7 GiB in total). The pinned test broker ships in
  `.github/actions/setup-nats-server` with a verified SHA-256 digest, and
  the nightly and release integration lanes run the real-broker suite.
- Operators gain the migration and rollback procedures in `docs/nats.md`.
  Rollback uses the same stopped and unbound gate and never restores an old
  delivery cursor.
- Safety invariants stay: sign-off gates fail closed, logs expose bounded
  failure categories instead of exception text, and metrics keep their
  closed label domains.
- Shutdown closes connections promptly: pull subscriptions unsubscribe
  before a bounded drain, because an unbounded `nc.drain()` waits out the
  client `drain_timeout` when a pull pending queue holds undelivered
  messages.

## Alternatives considered

- Keep at-most-once acknowledgement. Rejected: no audit trail and no replay
  source, which is the gap reported in #2395.
- Withhold acknowledgement on failure without a store. Rejected: a
  permanent failure loops the broker redelivery budget without a durable
  record of the attempts.
- Dead-letter records without a CAS ledger. Rejected: restarts and
  concurrent workers then risk duplicate handler invocation and duplicate
  dead-letter publication, because only the ledger serializes ownership.
