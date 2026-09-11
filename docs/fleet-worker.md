# Fleet worker

The Fleet worker owns one Codex 0.153.4 app-server process. It accepts admitted
commands through a private Unix socket. Agamemnon owns task admission. Keystone
supplies the authenticated transport. The worker does not discover issues, assign
tasks, change state labels, or run another task queue.
The current CLI supports provider startup and inspection. Session admission is
disabled on macOS and Linux until an enforced execution boundary is available.
The deployment steps below describe that later enabled mode; they do not bypass
the current gate.

## Start and attachment

1. Provision an isolated Linux worker boundary, then install the pinned
   Hephaestus package in Python 3.13. Native macOS session admission is disabled:
   the pinned process sandbox grants shared scratch access. Use the `automation`
   extra when the deployment also needs the existing issue pipeline. Linux
   container/VM enforcement and cluster mounts still require deployment validation.
2. Establish native Codex authentication independently in the worker's private
   `CODEX_HOME`, with `cli_auth_credentials_store="file"`. Do not copy another
   runtime's credential files. The provider explicitly selects file storage.
3. Create a private state directory and the admitted workspace directories.
   Keep state, authentication, and attachment-spool directories outside shared
   temporary trees. State, authentication, and workspace roots must not overlap.
4. Start the worker through the deployment's process supervisor:

   ```sh
   hephaestus-fleet-worker serve \
     --state-dir /private/fleet/state \
     --workspace-root /private/fleet/workspaces \
     --codex-home /private/fleet/codex \
     --worker-id laptop-1 --pool-id laptop --host-id laptop \
     --generation 1 --capacity 12
   ```

5. Attach the authenticated allocation transport to this command:

   ```sh
   hephaestus-fleet-worker attach --state-dir /private/fleet/state
   ```

`attach` exchanges one JSON request and one JSON response per line. Each request
uses a new local socket connection. The socket is `state-dir/worker.sock`, with
mode `0600`. It is never exposed as a TCP listener. Commands run serially; Codex
turns continue concurrently in their separate conversations.
Use `--codex-bin /absolute/path/to/codex` when the pinned executable is outside
the fixed runtime search path. Wrapper scripts must have their interpreter on
that registered search path; the worker does not inherit the operator's `PATH`.

`inventory --state-dir PATH` reports worker and session identities. The command
`events --state-dir PATH --after N` returns at most 500 metadata events. Its cursor
is local to one retained worker journal. It is not a global event sequence.

## Command contract

Every mutating command has these fields:

```json
{
  "schema": "hi/fleet/v1",
  "commandId": "command-1",
  "idempotencyKey": "assignment-1",
  "workerId": "laptop-1",
  "generation": 1,
  "targetKind": "sessions",
  "targetId": "session-1",
  "operation": "start",
  "workspace": "issue-1",
  "agentId": "agent-1",
  "taskId": "task-1",
  "sessionId": "session-1",
  "executionId": "execution-1",
  "stage": "implementation",
  "issueRefs": ["HomericIntelligence/Hephaestus#1"],
  "payload": {}
}
```

| Operation | Payload | Result |
|---|---|---|
| `start` | Workspace and assignment identity shown above | Creates one durable provider conversation |
| `input` | `text` | Starts a turn, or steers the observed active turn |
| `respond` | `requestId`, `response` | Answers a request owned by this session |
| `interrupt` | Empty object | Requests a turn stop; waits for provider confirmation |
| `cancel` | Empty object | Requests a turn stop with cancellation intent |
| `resume` | Empty object | Reacquires capacity for an interrupted session, or loads a disconnected session |
| `drain` | Empty object | Rejects new sessions and new user input |

An acknowledgment contains `commandId`, `eventId`, `workerId`, `generation`,
`targetKind`, `targetId`, `status`, and `receipt`. The `status` describes the
control command. A successful thread start or input command does not complete
the assigned issue. Interrupt and cancel return `accepted` until an actual
`turn/completed` notification supplies the outcome. An idle cancellation first
reads the provider thread and requires an actual `idle` status. Both stop paths
then clean and inspect background terminals. Only a confirmed empty inventory
permits release and a correlated stop fact. An idle cancellation then returns
`completed`.

Assignment fields can also appear in `payload` for an attached client. Duplicate
fields must agree with the controller envelope. Conflicts fail before dispatch.

The private attachment also accepts `inventory`, `events` with `after`, and
`requests` with `targetId`. Request details can contain private command or question
text. Keep them on the authenticated UI attachment. Do not publish them as
Keystone telemetry. The trusted attachment must resolve private input references;
the worker does not treat a reference as prompt text.

The private `requests` reply contains `id`, `method`, `params`, and `sessionId` for
each pending provider request. Preserve the integer or string type of `id`.
Command and file approvals support `response: {decision: "accept" | "decline" |
"cancel"}`. Session-wide and policy-amendment decisions are not supported.
User-input replies use `response: {answers: {QUESTION_ID: {answers: [STRING]}}}`.
The caller must recheck the current worker, generation, session, provider thread,
turn, and pending request before it submits a response. Pending requests are
private in-memory state; they are not restored from the journal after restart.
The worker rejects approvals from finished or different turns and ignores their
late item-activity notifications.
File approval parameters contain the item identity, reason, and optional grant
root, but no diff. A UI must obtain private item evidence before it presents a
file-change approval as a reviewed diff.

## Activity and recovery

Activity events have `eventId`, `workerId`, `generation`, `sourceSequence`, `seq`,
`targetKind`, `targetId`, `kind: activity`, and `event`. The event holds task and
issue references, logical agent/session/execution identity, host/pool/allocation,
stage, provider IDs, `observedAt`, `activity`, and `waitingReason`.

The observed activities are `model_working`, `tool_running`, `waiting_approval`,
`waiting_input`, `idle`, `disconnected`, and `unknown`. An idle turn can have a
`completed`, `failed`, `interrupted`, or `cancelled` outcome. None releases the
canonical issue claim. A confirmed interrupt releases active execution capacity
and retains workspace ownership. Resume reacquires capacity before it loads the
same conversation. It does not submit a prompt or start a turn. A confirmed
cancellation makes that session terminal and releases its local workspace and
capacity reservations. The journal retains the cancelled session identity.
Unknown or disconnected work retains its reservations.
Stop confirmation includes `backgroundCleanup: confirmed_empty`. Cleanup errors,
nonempty inventory, or expiration of its one-second budget leave activity
`unknown`, reason `background_cleanup_unconfirmed`, and reservations intact.
Resume requires reconciliation after this uncertainty. Pending requests from a
stopped turn are removed; an old approval cannot reactivate it.
Resume and each new input invalidate previous cleanup evidence before provider
dispatch. Natural completed/failed turns inspect the current background inventory
without terminating interactive services. Only a fresh empty inventory reports
`confirmed_empty` for that provider turn; nonempty or unavailable inventory
reports `unconfirmed` and blocks manual task resolution. These observations do
not themselves release the task claim or conversation reservation.
Prompt text, model output, shell command text, and credential values are excluded
from activity facts and command receipts.

Recognized text, reasoning, tool-output, and usage notifications refresh active
observations at most once per five seconds per session. The thread and turn IDs
must match. Waiting or idle sessions cannot become active from an old delta.
The provider reader first coalesces these frames into bounded metadata entries,
so these raw stream frames cannot fill the separate lifecycle queue. Text and token values
are discarded. Without another actual observation, the timestamp does not move.
A consumer must show an old observation as stale.

The worker flushes command digests before provider dispatch and receipts before
acknowledgment. Repeated keys return the retained result. Changed content with the
same key fails. An intent without a receipt reports `outcome_unknown`; it is never
replayed automatically. The local JSONL journal has one writer, a 64 MiB default
limit, and a 1 MiB record limit. A full or incomplete journal stops admission and
requires explicit recovery. The worker does not trim unresolved history.

A retained provider PID blocks restart while that process might still exist.
An authentication-owner lock also covers the provider process. A different
generation or an uncertain process-group cleanup requires explicit reconciliation.
The journal stores execution facts;
it is not another orchestration database.

## Permission and deployment gates

The adapter checks the exact CLI version. It disables both Codex multi-agent
features and `agents.enabled`. Per-session configuration selects a generated
named permission profile with `":minimal" = "read"` in its filesystem table.
`:minimal` is a filesystem selector, not a supported profile parent.
The profile grants workspace writes
and denies access to the authentication and receipt directories. Commands cannot
supply arbitrary provider configuration. The configuration fields follow the
[pinned Codex schema](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/config.schema.json).

Protocol tests use a real deterministic fixture subprocess. They do not establish
native authentication, actual sandbox enforcement, cluster connectivity, provider
capacity, or 108-agent performance. Those remain
deployment gates. The fixture cannot serve as infrastructure acceptance evidence.

The pinned macOS process sandbox adds shared temporary-directory access when
`:minimal` is selected; its filesystem helper does not add those process defaults.
The worker therefore rejects native macOS session admission with
`native_macos_requires_isolated_linux_worker`. A laptop Linux VM or container
boundary must separately demonstrate its filesystem and resource isolation.
Linux also rejects admission with `linux_execution_requires_verified_boundary`.
The platform name and a test report cannot enable execution. A shared container
around 24 conversations does not establish separate tool boundaries.
The worker does not expose a CLI bypass. See the pinned
[process policy builder](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/sandboxing/src/seatbelt.rs)
and [shell environment defaults](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/protocol/src/config_types.rs).

Provider `HOME`, XDG roots, and scratch live under its private
`CODEX_HOME/fleet-runtime`. Tools inherit no provider environment: each session
uses workspace-local `.fleet-runtime` directories, fixed tool paths, and disabled
shell profiles and snapshots. Exclude `.fleet-runtime` from source delivery and
build snapshots. These paths contain tool scratch, not provider credentials.
Existing non-Fleet environment builders are unchanged.

Codex turn interruption can leave background terminals running. The worker now
uses `thread/backgroundTerminals/clean` followed by a bounded inventory check
through `thread/backgroundTerminals/list`. A cleanup acknowledgment alone is
insufficient. This checks Codex's tracked terminal inventory; independently
detached processes and container/allocation cleanup remain deployment gates.

The Linux image must retain the complete pinned Codex platform payload, including
the adjacent `codex-resources/bwrap` helper. With that packaging corrected, the
September 11 no-auth container probe reached bubblewrap but reported a denied
`devpts` mount. The container remained non-root, read-only, without network access
or added capabilities. This result does not establish Linux profile enforcement.

## Contained tool environment contract

One external tool container per logical session can preserve five authenticated
app-server runtimes. Each container needs its own workspace, home, scratch, and
process boundary. Runtime credentials and container-control sockets must remain
outside those containers. The current selection adapter does not create these
containers or claim that their process boundaries are enforced.

`EnvironmentLease` binds worker, session, generation, environment ID, complete
container ID, image digest, workspace, and an absolute Podman launcher path.
`EnvironmentRegistry` rejects overlapping workspaces and repeated container or
environment IDs. It writes private `environments.toml` once and checks a digest
of all lease fields on reconstruction. An existing configuration change requires
reconciliation. The fixed attachment command is `podman start --attach
--interactive --sig-proxy=false CONTAINER_ID`.

The registry sets `include_local=false` and `default="none"`. The worker sends an
explicit singleton `environments` array on every `thread/start` and `turn/start`.
It validates the retained binding before a turn steer. The pinned provider can
select every registered environment when a request omits its selection, so a
default alone is insufficient. See the pinned
[environment selector](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/exec-server/src/environment.rs).

The pinned `thread/resume` schema has no environment override. Remote resume
therefore fails with `environment_resume_requires_reconciliation` before a
provider request. The current runtime must establish and verify the original
binding before that capability can be enabled. The default CLI exposes no
environment registry or isolation bypass.

The standalone `tests/integration/fleet_exec_server_probe.py` uses fixed
`initialize`, filesystem, process, termination, and stdio-shutdown requests in
an externally contained endpoint. It records actual process exit and detached
child survival separately. Its direct requests omit the platform sandbox to
measure the container boundary alone; this does not change the worker profile.
The wrapper must prove that forbidden synthetic markers exist in the separate
source container, bound the whole run, and remove only its owned test containers.
The result cannot authorize admission or prove a normal model tool route.

The September 11 paired container run passed both direct exec-server probes with
the pinned binary. Each endpoint read its own marker, denied the peer and private
authority markers, and observed tracked process exit and stdio shutdown. Each
still had one synthetic detached child after stdio shutdown. The probe then
stopped its own synthetic child. The external wrapper separately removed both
exact, still-running containers and observed their cgroups absent. This proves
those separate observations; it does not prove that container removal killed a
still-live detached child. The run used one CPU and one GiB per endpoint, no
network, private PID
namespaces, read-only roots, dropped capabilities, and no new privileges.

A separate focused disposal run then verified a live detached child by its
synthetic nonce, process start time, session, and cgroup. The wrapper removed
that exact container without first signaling the child. The child and parent
PIDs, container cgroup, and enclosing scope were absent before the child's
45-second self-limit. This demonstrates causal disposal in the tested engine.
The production Fleet supervisor does not yet implement that lifecycle.

Raw results and wrapper observations are retained in the private artifact
directory `image-build-20260911/exec-server-pair-01`. Its source manifest states
that the frozen worker wheel predates the Linux admission gate. The focused
disposal output is under `image-build-20260911/live-child-disposal-01`. Do not use
that older image for Fleet issue execution. The later `refresh-01` arm64 and
amd64 images include the Linux admission gate and lease module. Both passed
worker startup, inventory, and private-socket admission rejection with no auth
file and zero sessions. Those captures bind the frozen source manifest
`fce37a88e8ccd150dacdfccb01e571c80811a0e88d51238d55f169e725b78e45`.
Later source edits require another explicit freeze before image validation.
Fleet execution acceptance still requires the enforced adapter.

Before admission, deliver a container supervisor that creates and inspects each
immutable boundary, verifies the selected environment, and observes its complete
cgroup after disposal. Missing or uncertain disposal evidence must retain the
workspace and execution reservation. Codex's tracked-terminal list is not this
evidence. Normal model tool calls can also require bubblewrap inside the tool
container; the direct exec-server probe cannot establish that compatibility.

The pinned provider has a possible next adapter contract:
`turn/start` accepts `sandboxPolicy: {type: "externalSandbox", networkAccess:
"restricted"}`. This is a thread/turn policy, not an environment registration
field. It can leave shell and patch containment to an external executor instead
of starting another platform sandbox. See the pinned
[permission profile](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/protocol/src/models.rs)
and [turn parameters](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/app-server-protocol/src/protocol/v2/turn.rs).
This policy is not enabled here. A later adapter must verify the complete
container boundary and singleton remote selection before it can use that policy.
The external policy's restricted-network value relies on that boundary for
enforcement. A managed-network requirement can still need a platform sandbox.
It must also prove that no local tool route remains available and that engine
credentials/configuration stay outside tool containers. Keep the current native
and shared-runtime profiles unchanged.
The installed Codex 0.153.4 parser/status canary independently recognized both
generated environment IDs as pending, with local and missing IDs unknown. It
used an empty private home and passive `environment/status` requests; no engine,
thread, tool, authentication, or model operation ran. Actual selection and use
of the contained endpoints remain separate gates from this parser check and the
fixture protocol tests.

The separate installed-binary metadata canary verifies the effective profile,
workspace roots, and approval policy with a new empty private `CODEX_HOME`.
It permits only `initialize` and `thread/start`; it cannot submit a model turn.
Codex 0.153.4 accepted an equivalent generated profile in that host canary.
This example uses the paths from the launch command above:

```toml
[permissions.fleet.filesystem]
":minimal" = "read"
"/private/fleet/workspaces/issue-1" = "write"
"/private/fleet/codex" = "deny"
"/private/fleet/state" = "deny"

[permissions.fleet.network]
enabled = false
```

The worker generates the profile for each assigned workspace. It does not require
an operator to write this file. The native canary suite uses a fresh empty home
under the private `FLEET_NATIVE_PROBE_ROOT` directory, outside shared temporary
trees. Set `FLEET_CODEX_METADATA_CANARY=1` and optionally
`FLEET_CODEX_NATIVE_BIN=/absolute/path/to/the/pinned/native/executable`, then run
`uv run pytest tests/integration/test_fleet_codex_metadata_canary.py
--override-ini=addopts= --no-cov -s`.
An outer process sandbox can reject Codex's nested `sandbox-exec` setup on macOS.
In that case, the same no-auth canary command needs host execution approval; do
not weaken the Fleet profile. The process test uses one fixed `command/exec`
request with generated markers only. Independent execution observed workspace
access, denied sibling/authority reads, private tool home, and no inherited
`CODEX_HOME`; it also confirmed shared scratch reads/writes, supporting the native
admission rejection. It performs no model turn, login, or refresh operation.

The opt-in cross-component test consumes an artifact generated by Agamemnon's
`just fleet-export PATH` recipe. Set `FLEET_CONTROLLER_CONTRACT=PATH` and run
`uv run pytest tests/integration/test_fleet_controller_contract.py
--override-ini=addopts= --no-cov` in this repository. The fixture uses real
controller serialization and a deterministic provider process. It maps only the
physical workspace root and resolves the fixture's private input reference.

The current adapter slice does not connect the existing Hephaestus issue-stage
callbacks, heavy-build MCP recipes, or private terminal history. Add those through
their owning interfaces before claiming the full Fleet workflows are complete.
Keep existing `exec` integrations and label/publication rules in effect.
