# NATS JetStream Configuration

`hephaestus.nats` is optional and disabled by default. When a subscriber is
enabled, production deployments should use TLS and provide certificate material
as runtime file paths.

## TLS Deployment

```yaml
nats:
  enabled: true
  url: "tls://nats.example.com:4222"
  tls: true
  tls_ca_file: "/run/secrets/nats/ca.pem"
  tls_cert_file: "/run/secrets/nats/client.pem"
  tls_key_file: "/run/secrets/nats/client.key"
  tls_hostname: "nats.example.com"
```

Load the file at the owning application boundary and pass its `nats` mapping to
`load_nats_config`; ambient NATS settings are not read:

```python
from hephaestus.config import load_yaml_config
from hephaestus.nats import load_nats_config

document = load_yaml_config("/run/hephaestus/config.yml")
nats_config = load_nats_config(document.get("nats", {}))
```

Certificate and key files are runtime secrets. Do not commit certificate or
private-key contents to this repository.

## TLS-First Endpoints

Some NATS deployments require TLS before the INFO protocol handshake:

```yaml
nats:
  tls_handshake_first: true
```

## Plaintext Exception

Plaintext `nats://` is intended only for local development or explicitly
isolated test deployments. Non-local plaintext URLs are rejected for enabled
subscribers unless the exception is explicit:

```yaml
nats:
  enabled: true
  url: "nats://dev-broker.internal:4222"
  tls: false
  allow_plaintext: true
```

For a local broker without TLS, use a loopback URL and disable TLS:

```yaml
nats:
  enabled: true
  url: "nats://127.0.0.1:4222"
  tls: false
```

If a deployment breaks after adopting the TLS default, the short-term rollback
is to select a prior configuration file or explicitly configure a loopback
development broker with `tls: false`. A non-local plaintext exception requires
`allow_plaintext: true` in the same operator-owned YAML while certificate
provisioning is repaired.

## Durable failure delivery

The subscriber retains decode and handler failures. It never acknowledges a
failed delivery before the failure record is durable. A message that exhausts
the bounded processing budget moves to a dead-letter record. The retained
record serves as the audit trail and as the replay source. There is no
unbounded poison-message redelivery loop from the listener: a permanent
failure terminates its delivery after the dead-letter record is written.

### Processing budget

The delivery defaults bound every retry path:

| Field | Default | Role |
| --- | ---: | --- |
| `max_processing_attempts` | 3 | Processing attempts before dead-letter handoff |
| `redelivery_backoff_seconds` | `(1.0, 5.0)` | NAK delay schedule between attempts |
| `ack_wait_seconds` | 30.0 | Consumer redelivery timer |
| `heartbeat_interval_seconds` | 10.0 | Handler liveness heartbeat; must be <= `ack_wait_seconds / 3` |
| `ledger_lease_seconds` | 30.0 | Ownership lease for restart takeover; must be >= 2x heartbeat |
| `max_deliveries` | 6 | Broker redelivery budget; must exceed `max_processing_attempts` |
| `max_failure_payload_bytes` | 1048576 | Largest retained payload (exactly 1 MiB) |
| `auto_provision_failure_resources` | true | Provision the four streams and advisory subjects |
| `migrate_incompatible_consumers` | false | Gate the one-time consumer cutover |

The listener evaluates a decode failure without handler dispatch and a
handler failure inside a heartbeat context that renews the delivery and the
ledger lease. A success completes the ledger record, then acks the delivery,
then closes the record as acked. A restart resumes the ledger state instead
of the in-memory attempt count.

### Topology

Auto-provisioning creates four streams once and validates them non-destructively
on every start:

| Stream | Subject prefix | Contents |
| --- | --- | --- |
| `HEPHAESTUS_NATS_FAILURES` | `hephaestus.nats.failure.>` | One dead-letter envelope per failure event |
| `HEPHAESTUS_NATS_DELIVERY_LEDGER` | `hephaestus.nats.ledger.>` | One CAS record per event identity |
| `HEPHAESTUS_NATS_ADVISORIES` | `$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.<stream>.<consumer>` | Server max-delivery advisories for the configured bindings |
| `HEPHAESTUS_NATS_REPLAY_RECEIPTS` | `hephaestus.nats.replay.receipt.>` | One replay receipt per (failure id, request id) pair |

The event identity is the durable (stream, consumer, stream sequence) triple.
Every ledger write carries the `Nats-Expected-Last-Subject-Sequence` header
of the prior record version, so two workers converge on one owner.
The dead-letter stream sets `max_msg_size` to 1,414,488 bytes, which holds an
exact-bound payload envelope.

### Disposition rules

- Success: the ledger reaches `completed`, `ack_sync()` confirms the delivery,
  and the record closes as `acked`.
- Failure below the budget: the delivery receives `nak()` with the scheduled
  backoff and tries again later.
- Failure at the budget: the envelope publishes idempotently by identity, the
  delivery receives `term()`, and the record closes as handed off.
- A payload above the capture bound, or metadata that cannot produce a durable
  identity, means the listener takes no disposition. The broker redelivers up
  to `max_deliveries` and then fires the max-delivery advisory. The advisory
  reconciler fetches the exact advisory `stream_seq` and writes the durable
  record with `handler_outcome_unknown`. A found source message stays
  replayable; a missing one stays metadata-only.

### Broker requirements

- `max_payload` must exceed 1,414,488 bytes to admit a full-size envelope.
- The JetStream store limit must exceed the declared stream reservations
  (about 1.7 GiB in total). A limit at or above 2 GiB is the safe minimum.
- TLS deployments retain the certificate requirements from earlier sections.
- ACL profiles need publish and subscribe rights on the source subjects and on
  the four failure-resource subject prefixes, JetStream stream create, info,
  and update calls, consumer create, info, and delete calls, message get, and
  the advisory subjects for the configured bindings.

### Replay

`FailureReplayer.replay(failure_id, request_id)` re-emits one durable failure:

- The replayer publishes the retained payload to its source subject with
  deterministic replay headers and `Nats-Msg-Id` for broker deduplication.
- The receipt transitions `publishing` and then `completed` under CAS. A
  restart that repeats the same request id returns the original publish
  acknowledgement without a second publication. A stuck `publishing` receipt
  reports `UNKNOWN` instead of risking a duplicate publication.
- A missing failure record raises `ReplayNotFoundError`. A non-replayable,
  oversized, or undecodable record raises `ReplayNotReplayableError`.
- Every replayed publication enters the source stream once, so a restarted
  subscriber invokes the handler exactly once per publication.

### Consumer migration

An incompatible pre-existing durable (for example an `AckPolicy.NONE`
leftover) fails the startup check by default and stays untouched. The one-time
cutover is:

1. Stop every process bound to the durable and verify `push_bound = false`.
2. Snapshot the consumer configuration and stream state.
3. Start once with `migrate_incompatible_consumers=true`.
4. Verify `AckPolicy=explicit` and the resume sequence at
   `current_ack_floor + 1`.
5. Set the flag back to `false` for normal operation.

Migration deletes and recreates the consumer only while it is unbound, and it
restores the snapshot when the recreation fails.

### Rollout and rollback

Rollout is automatic: provisioning is non-destructive, new deployments start
on the explicit consumers, and a legacy incompatible durable completes the
migration procedure once.

Rollback uses the same stopped and unbound gate: snapshot the explicit ack
floor, delete the replacement consumer, recreate the saved legacy
configuration at `current_ack_floor + 1`, and restart on the legacy build.
Rollback never restores an old delivery cursor. Failure records, ledger rows,
advisories, and replay receipts stay durable and restart-safe during the
rollback.

Logs and classified health fields remain observability signals: they expose a
bounded failure category such as `handler_error` or `connection_error` and
never publish exception text or tracebacks. Code that inspects the raw
`last_error` object must not copy its text into externally visible
diagnostics.

## Metrics cardinality

NATS metrics use closed label domains. Subscriber `state` permits exactly
`initializing`, `connected`, `disconnected`, `stopping`, `stopped`, and
`error`. Circuit-breaker `state` permits `closed`, `open`, and `half_open`.
Error `kind` permits `connection`, `terminal`, `handler`, and `decode`.

| Metric | Labels and allowed values | Series cap |
| --- | --- | ---: |
| `hephaestus_nats_subscriber_state` | `state`: `initializing`, `connected`, `disconnected`, `stopping`, `stopped`, `error` | 6 |
| `hephaestus_nats_subscriber_circuit_breaker_state` | `state`: `closed`, `open`, `half_open` | 3 |
| `hephaestus_nats_subscriber_errors_total` | `kind`: `connection`, `terminal`, `handler`, `decode` | 4 |
| `hephaestus_nats_subscriber_messages_total` | — | 1 |
| `hephaestus_nats_subscriber_last_message_timestamp_seconds` | — | 1 |

Rejected new series are reported by the shared
`hephaestus_metrics_series_overflow_total` counter; admitted series remain
updateable and are never evicted.
