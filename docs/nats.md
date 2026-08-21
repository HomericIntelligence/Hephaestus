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

## Failed-message retention

`NATSSubscriberThread` deliberately provides best-effort, at-most-once delivery
for non-critical events where losing a failed message is acceptable or the
authoritative upstream source can reconstruct and re-emit it.

- A malformed UTF-8 or JSON payload is warning-logged, acknowledged, and never
  passed to the handler.
- A handler exception remains available through the in-process `last_error`
  property and is acknowledged without updating `last_message_at`.
- Health responses and logs expose only a bounded failure category such as
  `handler_error` or `connection_error`; they never publish exception text or
  tracebacks.
- Diagnostic URLs retain the broker scheme, host, port, and path while removing
  URL userinfo, the complete query string, and fragments. The original URL is
  still used internally when connecting.

Acknowledged failures are **not retained** by Hephaestus. There is no built-in
dead-letter queue (DLQ), replay store, or operator replay command. Logs and
`last_error` are observability signals, not durable storage.

Logs and classified health fields are observability signals, not durable
failure storage. Code that inspects the raw `last_error` object must not copy
its text into externally visible diagnostics.

Workflows requiring durable processing, audit trails, or operator-driven replay
must not use this abstraction unchanged. They require a consumer that persists
the raw message and failure to a DLQ or replay store before acknowledging it.
Simply withholding acknowledgement is not an adequate substitute because a
permanently malformed or handler-rejected message can create an unbounded
poison-message redelivery loop.

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
