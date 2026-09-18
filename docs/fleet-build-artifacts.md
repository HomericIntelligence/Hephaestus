# Private Fleet build logs

`hephaestus-fleet-build-artifacts` serves retained build output over local
HTTPS. Agamemnon reads this service through its existing private log client.
Odysseus reads through Agamemnon. The service does not admit, start, cancel,
or complete work. It does not publish results or set `collectionVerified`.

This service is in the optional `hephaestus.automation` product layer. Its
command has Provisional status. Producer registration and actual
Agamemnon-to-service deployment need separate verification. Keep controller
log reads disabled until those gates pass. Local service tests do not prove
producer capture, build isolation, cluster execution, or Fleet performance.

## Private configuration

Select a configuration file with `--config`. The command does not read
configuration or credentials from `HEPH_*` environment variables. Help and
version do not read private files or open a listener.

All input paths must be absolute. The path walk must contain no symbolic
links or `..` components. Each input must be a regular file owned by the
effective user, with one hard link and mode `0400` or `0600`. Its immediate
directory must have the same owner and mode `0500` or `0700`. The loader
checks file metadata before and after reading. It refuses platforms without
the required POSIX descriptor operations.

The configuration is a JSON object with exactly these fields:

| Field | Value |
| --- | --- |
| `schema` | `hi/hephaestus/build-artifact-service/v1` |
| `host` | Exactly `127.0.0.1` or `::1` |
| `port` | Integer in `0..65535`; zero selects an available port |
| `certificateFile` | Absolute path to the operator's PEM certificate chain |
| `privateKeyFile` | Absolute path to the matching PEM private key |
| `bearerFile` | Absolute path to the dedicated bearer credential |
| `registrations` | Array of the registrations described below; it may be empty |

The bearer file contains 1 through 4096 printable ASCII bytes. Spaces,
newlines, and other control characters are invalid. The service does not
trim the value. Do not put the credential on the command line.

Supply a certificate whose IP SAN contains the selected literal address.
Use a private key that does not need a password prompt. The service loads
checked private copies into a TLS server context and removes those copies
after loading. TLS 1.2 is the minimum version. The service does not generate
certificates, install trust, bind remote addresses, or provide plaintext
fallback. It does not enable ambient TLS key logging.

## Registration ownership

An operator or a separately verified producer owns registration. Obtain the
expected commitments from the independently retained result record. Hashing
an arbitrary directory and declaring its hashes trusted does not establish
producer identity. Do not create a replacement receipt or invent capture
evidence to make registration succeed.

Each registration has exactly these fields:

| Field | Commitment |
| --- | --- |
| `directory` | Absolute private result directory |
| `receiptDigest` | SHA-256 of the exact retained `receipt.json` bytes |
| `identityDigest` | SHA-256 of the complete canonical receipt `identity` object |
| `buildId` | Exact admitted build identifier |
| `attempt` | Positive integer attempt |
| `snapshotDigest` | Exact `identity.snapshot.manifestDigest` |
| `workerId` | Exact `identity.policy.allocation.workerId` |
| `allocationId` | Exact `identity.policy.allocation.id` |
| `generation` | Exact positive `identity.policy.allocation.generation` |
| `logs` | Exactly `{reference,digest}` from the retained terminal log record |

Digests use 64 lowercase hexadecimal characters. Identifiers match
`[A-Za-z0-9][A-Za-z0-9_-]{0,127}`. Boolean and floating-point values cannot
replace integer identities. The log reference must be `output-` followed by
the receipt's `leaseId`.

The loader reads four fixed members: `receipt.json`, `manifest.json`,
`stdout.txt`, and `stderr.txt`. Requests cannot select paths or register
directories. The receipt uses `hi/hephaestus/build-result/v1` and contains
exactly `schema`, `identity`, `argv`, `outcome`, `exitCode`, `cleanup`, `files`,
and `artifacts`. This profile requires a terminal outcome with a consistent
exit code and an empty `artifacts` array. The complete receipt and identity
digests bind the retained producer fields. The service does not repeat the
producer's admission or cleanup decision.

Each `files` descriptor binds the fixed member path, byte count, and digest.
The source manifest digest must also equal `snapshotDigest`. Both streams
must contain valid UTF-8. Canonical JSON uses sorted keys, compact separators,
UTF-8 characters without ASCII substitution, no nonfinite numbers, and no
final newline. Duplicate keys and excessive nesting are invalid.

The log digest is SHA-256 of canonical `{"stdout": stdout, "stderr": stderr}`
JSON. It is not the receipt digest or the source manifest digest. The page's
`manifest` field is this retained terminal `{reference,digest}` pair.

Duplicate directories and conflicting registrations for a build attempt
fail before listening. A new generation or snapshot cannot replace that
attempt within one configuration. After validation, the service retains
immutable stream bytes in memory. It releases receipt and manifest parse
data. Later file changes do not alter the running historical view. Restart
loads and checks the files again against the selected commitments.

## Read interface

The service accepts one authenticated, bodyless GET per connection:

```text
GET /v1/fleet/build-jobs/{buildId}/logs?attempt={attempt}&snapshotDigest={digest}&stream={stream}&after={after}&limit={limit}
Authorization: Bearer {dedicated credential}
```

Supply every query field exactly once. Unknown fields and repeated fields
are invalid. Integers use decimal digits without a sign or leading zeros.
`stream` is `stdout` or `stderr`; `after` is in `0..2^63-1`; `limit` is in
`1..65536`. Authentication precedes registration lookup. There is no HTTP
registration, manifest download, receipt download, health, or metrics route.

A successful page has exactly `schema`, `buildId`, `attempt`,
`snapshotDigest`, `stream`, `after`, `next`, `data`, `chunkDigest`, `complete`,
`truncated`, and `manifest`. Its schema is `hi/fleet/build-logs/v1`.
Offsets and limits count UTF-8 bytes. `chunkDigest` hashes the exact page
bytes. `next` equals `after` plus their byte count.

A page ends at the last complete UTF-8 scalar that fits. At retained EOF,
the response has empty data, the empty-byte digest, and an unchanged cursor.
`complete=true` means that the page reaches retained EOF. Every page sets
`truncated=true`, which means that original capture may be truncated. The
current producer receipt cannot prove complete original capture. There is
no override to report false and no live append support.

| Backend status | Meaning |
| --- | --- |
| `200` | Valid retained page |
| `400` | Invalid query, body declaration, or cursor inside a UTF-8 scalar |
| `401` | Missing, repeated, or incorrect bearer authorization |
| `404` | Unsupported route or no matching build, attempt, and snapshot |
| `409` | Cursor is beyond retained EOF |
| `422` | The limit cannot contain the next UTF-8 scalar |
| `431` | Request line and headers exceed their aggregate bound |
| `501` | Unsupported HTTP method |
| `503` | The encoded page exceeds the response bound |

Errors use a fixed message without private input. A failed TLS handshake,
expired connection, or exhausted connection capacity can close the
connection without an HTTP response. Agamemnon preserves backend `409`.
It maps other backend failures, including `400` and `422`, to `503`.

## Bounds and lifecycle

| Resource | Fixed bound |
| --- | --- |
| Configuration | 1 MiB |
| Certificate and private key | 1 MiB each |
| Receipt and source manifest | 4 MiB each |
| Registrations | 256 |
| Retained stdout and stderr | 64 MiB across all registrations |
| Active connections and listener backlog | 8 each |
| Request line and headers | 16 KiB total |
| Encoded page | 400000 bytes |
| Connection deadline | 2 seconds, including handshake and request handling |
| Shutdown deadline | 3 seconds |

The receipt's per-attempt `outputBytes` limit also applies when it is lower.
The retained-byte bound is not a process-memory bound. Parsing and response
construction need additional memory. The loader processes one bundle at a
time; log hashing avoids a full escaped copy of both streams.

## Operator procedure

1. Obtain the retained bundles and independent registration commitments.
   Keep the bundles outside build-child write authority. Prepare the private
   configuration, certificate, key, and bearer files with the required modes.
2. Start the service through the installed command. This path is an example;
   replace it with the actual private configuration path:

   ```bash
   uv run --locked hephaestus-fleet-build-artifacts --config /absolute/private/service.json --json
   ```

3. Read the readiness object. It contains `status`, `host`, and the assigned
   `port`. All registrations and TLS inputs are checked before listening.
   When configured port is zero, use the reported nonzero port for the client.
   The process waits for explicit shutdown; it has no automatic runtime limit.
4. After the separate producer and deployment gates pass, select Agamemnon's
   private backend file with `AGAMEMNON_FLEET_BUILD_ARTIFACTS`. Its closed
   object contains `schema: hi/fleet/build-artifacts/v2`, `origin`, `key`, and
   `caCertificatePem`. Use the service's exact HTTPS literal address and port,
   the same dedicated bearer value, and one trusted PEM certificate. This
   client configuration schema differs from the service configuration above.
   Agamemnon requires its authenticated API and durable persistence. See the
   [bound client contract](https://github.com/HomericIntelligence/Agamemnon/blob/580f1c320c5e7f0a5685cef78d7119cfccb0c7b1/docs/fleet.md).
5. To reload files or renew the service certificate, send `SIGINT` or `SIGTERM`
   and wait for exit. Shutdown closes the listener and active connections and
   waits for its threads. Start a new process to load the new inputs. A trust
   anchor change also requires a coordinated private client configuration
   update and Agamemnon restart. Readiness does not prove client trust.
6. To disable or roll back, stop the service, remove the controller's backend
   selection, and restart the controller. Preserve retained bundles. No work
   state migration is required. Startup and shutdown failures have nonzero
   outcomes; investigate the private inputs before retrying.

Snapshot and supervisor work remains with
[issue 3231](https://github.com/HomericIntelligence/Hephaestus/issues/3231) and
[issue 3232](https://github.com/HomericIntelligence/Hephaestus/issues/3232).
Automatic producer registration, independent collection, and deployment are
separate gates. A registered historical view grants no worker execution.
