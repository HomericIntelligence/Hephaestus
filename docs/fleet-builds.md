# Fleet build supervisor

The [build supervisor](../hephaestus/automation/fleet_build_supervisor.py) owns
one subordinate build attempt. Agamemnon owns admission and the run grant.
The supervisor owns durable local order, verified source restoration, and the
execution lease. It does not create a task queue or invoke a model.

The first profile is `hephaestus-test-unit-v1`: `just test-unit` with empty
parameters, on Linux/aarch64 with zero GPUs. The [command validator](../hephaestus/automation/fleet_build_contract.py)
checks the exact policy, allocation, parent claim, snapshot, and typed generation.
It rejects caller shell, environment, workspace, and tool overrides. Policy
digests use sorted compact UTF-8 JSON without a final newline. Snapshot manifests
use their separate versioned encoding.

## Service and authority

Construct `BuildSupervisor` with one private state directory, one private
workspace parent, and explicit operator-owned capabilities:

- `BuildClient` owns authenticated claim and fact delivery to the fixed
  controller. Each call receives an absolute monotonic deadline. Caller input
  cannot select a URL or credential.
- `BuildSnapshot` binds an operator-selected artifact and `SnapshotPolicy` to
  the [snapshot verifier](../hephaestus/automation/fleet_snapshot.py). Restoration
  checks actual manifest, archive membership, file bytes, and modes. The service
  also checks actual restored `justfile` and `uv.lock` hashes against the policy.
- `BuildExecutor` owns preparation, the fixed command attempt, and exact lease
  disposal. It must enforce and verify the required remote or container boundary
  and resource limits. The state directory, output records, and workspace parent
  must remain outside child write authority. A returned metadata object cannot
  establish this isolation by itself.
- `BuildResultPublisher` retains private result bytes before terminal fact
  delivery. A production command receiver must supply this capability. The
  existing core without a publisher can retain incomplete evidence, but cannot
  qualify a collected build result.

There is no default executor or host fallback. The current core does not ship a
qualified build executor. A test adapter that runs a fixed Python child exercises
the service contract; it does not qualify Linux, an image, or a real recipe.

`handle(command)` accepts the actual typed start or cancel envelope. A start call
may wait for one attempt. Another caller can submit cancellation concurrently.
`status()` returns bounded state without private output. `reconcile()` operates
on retained ownership and never starts another process. `close()` refuses to
release the journal while a synchronous operation remains active.

MCP and `BuildTestJob` adapters must share the ordinary Agamemnon build client.
Only the admitted-command receiver uses `BuildSupervisor` and the worker-only
grant bridge. Both integrations remain required product work. The project MCP
adapter has an optional runtime dependency; ordinary build execution must work
without starting MCP. The existing host `BuildTestJob` path does not become an
offloaded build through this module.
The posture in [ADR-0011](adr/0011-mcp-integration-posture.md) stays unchanged.

## SDK transport

[`BuildClientBridge`](../hephaestus/automation/fleet_build_client.py) implements
the synchronous claim/fact interface through one Agamemnon SDK client. Supply
an asynchronous client factory with the operator's fixed endpoint,
backend credential, and `trust_env=False`, plus the separate supervisor key.
Open, use, and close the bridge on one synchronous owner thread. It refuses
calls inside an active asynchronous event loop and calls after closure.

The bridge applies each caller's absolute monotonic deadline to one SDK call.
It does not retry a request or generate another claim or fact. The supervisor
retains the recovery decision. Client creation and closure have a configurable
SDK cancellation budget, defaulting to five seconds. This uses cooperative
async cancellation; a process owner must handle an unresponsive runtime.

The normal locked development environment pins the SDK to Agamemnon commit
`ef39b3506bb29debe7e728f33e8d80c0923a330c` in the `automation` dependency group.
This group does not change the published package extra. A package release
still needs an indexed SDK version containing these build methods before
ordinary package installation can supply this integration.

## Durable order

The supervisor reuses [WorkerJournal](../hephaestus/automation/fleet_journal.py).
It flushes each state transition before the corresponding external effect.

Both build owners use a shared private storage initializer. It requires an
existing private ancestor, creates missing directories with mode 0700, and
synchronizes each new directory name through its parent descriptor. It also
synchronizes retained journal bytes and the journal directory after the journal
files are opened. A failed initialization closes the opened journal and writer
lock, including failures from a directory context on exit. The supervisor uses
the same directory
initializer for its workspace parent before writing its owner binding. A retry
synchronizes the exact retained owner binding before it requests a grant. Output
publication synchronizes the output file and its directory before it sends a
terminal fact. These build-only helpers do not change other journal callers.

The local ordering tests observe real file and directory synchronization before
the first transport call and after a reported interruption on restart. They are
not power-loss experiments or scheduler qualification.

1. Validate the exact command and retain one generated claim ID.
2. Request the controller grant with that retained claim. Validate the full
   returned command, typed claim, and grant digest. Persist the grant before
   executor preparation or process creation.
3. Restore and verify source bytes under the exclusive workspace parent. Persist
   the exact execution lease before calling its preparation method.
4. Persist the start boundary before invoking the fixed executor command.
5. Require an exact owned disposal observation before creating a terminal fact.
   Persist the fact before publication.
6. Read and hash the bounded private output before sending its terminal
   reference. After a lost publication reply, verify those bytes again and
   resend the same retained fact. Do not run the recipe again or generate a
   new identity for that event. An accepted controller stop uses the separate
   cancellation recovery described below.

Each control call has a finite budget. The run call receives the remaining
recipe wall budget and output bound. There is no automatic retry loop. An
explicit grant retry uses the original claim, and a retained start boundary
always prevents a second process attempt.

## Cancellation and recovery

A cancellation retains the complete start identity plus the exact cancellation
command. The journal records this fence before the shutdown event is set.
Cancellation before executor preparation can prove that this owner started no
process. After preparation or start, cancellation requires disposal of the
retained lease and an observation of `confirmed_empty`.

A grant that returns after the fence cannot proceed to preparation or start.
The executor also receives the cancellation event at the start boundary. The
controller grant cutoff and remote process creation are not one atomic event.
The executor must honor a set event before process creation and own bounded
termination and reap after creation.

An uncertain start retains its lease. Redelivery and restart return
`reconciliation_required`; they do not repeat execution. Reconciliation can
confirm owned cleanup. Cleanup alone cannot reconstruct the lost exit result,
so it cannot produce a successful build fact. An uncertain cleanup retains the
reservation.

An accepted controller stop can arrive after a local terminal fact was retained.
Build-attempt journal version 2 preserves the exact prior event body in
`supersededTerminal` and records a separate stop-bound cancelled fact. It does
not rewrite the old event. Recovery rejects changed history, a version downgrade,
or a different stop for the same attempt. Version 1 remains readable.

The existing accepted stop envelope supplies the controller authority. If the
controller accepted the original terminal first, it rejects cancellation and
returns no accepted stop. The original terminal is then replayed unchanged.
If the controller accepted the stop first, recovery sends its cancelled fact
without another grant, restore, run, or release. A late publisher or controller
reply cannot replace or mark this newer terminal as published. A failed reply
leaves the stop ready for an explicit retry after restart.

Cancellation can win after the actual recipe completed. Its confirmed cleanup
permits the cancelled fact, but does not change the execution outcome. Completed
output and any completed receipt stay private and unchanged. The cancellation
fact has null receipt, log and artifact references and keeps
`collectionVerified=false`. It does not claim a cancelled execution receipt.

Recovery validates the complete retained command, typed claim, grant, execution
lease, phase, cancellation, and terminal identity before an external effect.
A cleanup marker before any lease is invalid unless cancellation is retained.
That exception permits recovery when cancellation saved cleanup but the next
terminal write was interrupted. It cannot authorize a later start.

The workspace parent is bound to one state directory. A held filesystem lock
excludes simultaneous owners, and the retained binding excludes a different
journal after restart. Keep this parent exclusive during restoration and child
handoff. These construction checks do not defeat hostile same-UID mutation;
the executor must enforce the separate child authority boundary.

## Scheduler-step ownership

[`SlurmStepOwner`](../hephaestus/automation/fleet_build_executor.py) implements
local durable order for one existing allocation and one execution lease. It
requires an explicit trusted `StepTransport`; no host or scheduler transport is
selected by default. This owner is not yet a concrete `SlurmPyxisBuildExecutor`.
A concrete transport must independently obtain live allocation, node, user,
incarnation, cgroup and step observations and verify the image, runtime, quota,
isolation and supervisor resource reserve before it can supply this capability.

The owner binds the complete validated lease and policy to one private journal.
It persists a unique launch nonce and start intent before creating a gated step.
It then validates and persists the returned exact allocation/step identity and a
release intent before allowing the fixed recipe. A lost or ambiguous start reply
permits lookup and disposal of that same nonce; it never permits resubmission or
recipe release after recovery. A retained terminal result is read and hashed
again before replay. Large output stays in a separate private file rather than
expanding the journal's per-record limit.

Disposal first persists a release fence. It can cancel only the exact retained
step, never the enclosing allocation. Both matching scheduler terminal state and
execution-node kernel emptiness are required for `confirmed_empty`. Local launcher
exit or an unavailable observation is insufficient. Closing this owner releases
its local journal; it does not claim remote cleanup. All capabilities receive the
same absolute deadline. An unresponsive remote/runtime capability still requires
its concrete process owner to enforce the deadline and reap its resources.

Version 2 step journals retain the exact validated cleanup observation. The
`evidence()` method reads that observation and the bounded actual result without
another execution or scheduler probe. Version 1 records contain only a cleanup
summary. They remain readable, but cannot qualify result evidence until a new
`dispose()` call observes the same fenced lease and step. This call cannot start
or release another process. Changed retained observations require reconciliation.

The step tests use actual gated local processes and the harmless producer fixture
Justfile. Their allocation and step identities are explicitly synthetic. They do
not qualify Slurm, Pyxis, the full Hephaestus recipe or an operational allocation.

## Output and evidence

Output is bounded and written to private supervisor storage. A terminal fact
contains exact command, attempt, worker, allocation, policy, snapshot, platform,
image, toolchain, outcome, and cleanup identities. It contains private references
rather than output text. Missing receipt or artifact references remain null.

A timeout observation has no invented exit code. It supports a `timed_out` fact
only after exact lease disposal. An output-limit exception retains uncertainty;
reconciliation can confirm cleanup but cannot reconstruct an exit result.
When a terminal fact contains a log reference, private output must be a bounded
regular file owned by the current user, without shared permissions or additional
hard links. The supervisor reads its bytes and compares their digest with the
retained reference. Changed or missing referenced output prevents publication.
Early cancellation can publish a null log reference without an output file.

[`PrivateBuildPublisher`](../hephaestus/automation/fleet_build_publication.py)
uses the actual step owner's evidence and the original verified snapshot. It
checks the exact command, lease, policy, result and cleanup identities. It then
creates a private bundle containing the canonical receipt, source manifest and
captured standard output and error. The first fixed profile declares these files
and an empty additional-artifact list; it does not infer artifacts from arbitrary
workspace files. Every file and directory is synchronized before references are
returned. The publisher uses the existing descriptor reader, snapshot manifest
rules and collector receipt schema.

The supervisor retains terminal output before it calls the publisher, then
retains the returned references before it sends a controller fact. A failed
publication or lost reply permits another publication attempt for those same
bytes. It cannot cause another grant, restore or process release. A retry checks
the complete private bundle again; changed bytes, links, permissions or members
require reconciliation. Cancellation before any step exists retains absent
receipt and artifact references instead of constructing a step observation.
Cancellation after a completed step also keeps its evidence separate, as
specified in [cancellation and recovery](#cancellation-and-recovery).

`collectionVerified` remains false. Neither an executor reply, a signal request,
an exit code, nor a generic Fleet acknowledgement verifies an artifact collection.
[`collect_build_result`](../hephaestus/automation/fleet_build_collection.py)
independently verifies a private `hi/hephaestus/build-result/v1` receipt and every
referenced log, source-manifest and artifact file. A safe opaque reference selects
one private directory; no URL or alternate transport is accepted. Closed identity
validation reuses the admitted command, parent, policy and snapshot rules. Reads
use held no-follow directory/file descriptors, current-user 0700 directories,
0600 regular files with one hard link, bounded streaming hashes and stable
readback. Logs and artifacts have separate aggregate limits from the admitted
policy. The receipt is limited to 4 MiB and at most 1000 artifacts.

The collector uses the real snapshot exporter to compare current eligible source,
including dirty tracked files, untracked files and modes, with the exact original
commitment. It rechecks all referenced output after that capture. An unchanged
source produces `verified_current`; a complete old result with changed source is
`historical`, never current-checkout success. Missing, malformed, aliased, changed
or incomplete evidence requires reconciliation. The caller must retain exclusive
source/evidence ownership throughout collection; these checks do not defeat a
concurrent hostile process with the same user authority.

The private result receipt binds build, attempt, command, lease, parent, full
policy, snapshot, fixed argv, outcome/exit, exact step and cleanup observations.
Its manifest/stdout/stderr and artifact descriptors contain relative paths,
byte lengths and SHA-256 digests. The trusted publisher must place these bytes
outside child write authority and obtain real cleanup observations. Reading a
fixture's scheduler fields does not turn them into operational proof. The separate
`hi/hephaestus/build-collection/v1` result retains identity, reference, outcome,
verified artifact descriptors, sourceCurrent and status. It does not set the
controller's `collectionVerified` flag.

A qualified executor, production transport, actual allocation startup, full
recipe execution and operational collection remain
required integration/acceptance steps. Remote HTTPS artifact retrieval remains
unavailable; the collector supports only an explicitly supplied private root.
The current registered recipe requires Linux/aarch64. Both requested `m1-builds`
and `m2-builds` pools declare Linux/amd64. Neither requested pool is qualified by
these controlled tests. A measured amd64 recipe and toolchain policy remain an
integration gate; the implementation does not change desired platform settings.

The [focused tests](../tests/unit/automation/test_fleet_build_supervisor.py) use
actual exported controller and snapshot fixtures. They distinguish controlled
process observations from synthetic allocation metadata. Their results do not
enable admission, change an image, or authorize a provider turn.
