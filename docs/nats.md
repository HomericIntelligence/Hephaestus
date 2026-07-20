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

Equivalent environment overrides:

```bash
export NATS_URL=tls://nats.example.com:4222
export NATS_TLS=true
export NATS_TLS_CA_FILE=/run/secrets/nats/ca.pem
export NATS_TLS_CERT_FILE=/run/secrets/nats/client.pem
export NATS_TLS_KEY_FILE=/run/secrets/nats/client.key
export NATS_TLS_HOSTNAME=nats.example.com
```

Certificate and key files are runtime secrets. Do not commit certificate or
private-key contents to this repository.

## TLS-First Endpoints

Some NATS deployments require TLS before the INFO protocol handshake:

```bash
export NATS_TLS_HANDSHAKE_FIRST=true
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

```bash
export NATS_URL=nats://127.0.0.1:4222
export NATS_TLS=false
```

If a deployment breaks after adopting the TLS default, the short-term rollback is
to point `NATS_URL` at a loopback development broker and set `NATS_TLS=false`, or
to set `NATS_ALLOW_PLAINTEXT=true` only for an explicitly isolated non-production
broker while certificate provisioning is repaired.

## Failed-message retention

`NATSSubscriberThread` deliberately provides best-effort, at-most-once delivery
for non-critical events where losing a failed message is acceptable or the
authoritative upstream source can reconstruct and re-emit it.

- A malformed UTF-8 or JSON payload is warning-logged, acknowledged, and never
  passed to the handler.
- A handler exception is recorded in `last_error`, logged with its traceback,
  and acknowledged without updating `last_message_at`.

Acknowledged failures are **not retained** by Hephaestus. There is no built-in
dead-letter queue (DLQ), replay store, or operator replay command. Logs and
`last_error` are observability signals, not durable storage.

Workflows requiring durable processing, audit trails, or operator-driven replay
must not use this abstraction unchanged. They require a consumer that persists
the raw message and failure to a DLQ or replay store before acknowledging it.
Simply withholding acknowledgement is not an adequate substitute because a
permanently malformed or handler-rejected message can create an unbounded
poison-message redelivery loop.
