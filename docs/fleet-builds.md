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
grant bridge. The ordinary service and project MCP adapter share a supplied
client and durable caller owner. The explicit `BuildTestJob` adapter borrows that
same owner through a supplied running loop and registered source/evidence leases.
The MCP runtime dependency is optional; ordinary build execution works without
starting MCP. A job without a Fleet selection keeps the existing host path.
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
This development-only pin does not enter the wheel's runtime requirements.
The `fleet-build-mcp` extra supplies `automation` and MCP 1.30.0. The caller
supplies its qualified SDK client and registered owner; the extra does not
construct a client or obtain credentials. Base and `automation` installs do
not require MCP. A future extra that supplies the SDK itself requires a reviewed
indexed SDK release before that runtime dependency can be published.

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

When the executor run returns or raises, the supervisor attempts disposal of
that exact lease before returning control. Disposal has a separate five-second
budget. It does not extend the recipe deadline or make an expired result
eligible for collection. A failed run without a retained exit result remains
unresolved after confirmed cleanup. If disposal cannot confirm an empty
execution, the journal retains ownership for explicit reconciliation.

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

Publication and `PrivateBuildPublisher.read_result()` acquire an exclusive
flock on the actual private evidence-root descriptor. Separate publisher and
reader instances use this same kernel lock. Acquisition uses the operation's
deadline; result reads also observe shutdown. The read capability checks the
existing bundle before and after collection under that lease. It does not
publish, recreate missing files, or request another execution.

`BuildSupervisor.result_handoff()` supplies the trusted local result capability
for a registered build context. It derives the expected identity and reference
from its healthy, acknowledged journal state and compares the controller
observation with that state. It requires complete references, confirmed cleanup,
and no cancellation, reconciliation reason, or active execution. An acknowledged
failed or timed-out recipe can supply evidence without becoming a successful
build. A publisher that only supports publication remains valid for execution,
but cannot supply this read capability.

A short supervisor state lock installs a result-read pin. The pin stays active
through collection and pending receipt capture without holding that state lock
across file I/O. Start, cancellation, reconciliation, and close refuse a
conflicting pin before changing state. Exit checks journal health and the exact
retained state while the evidence lease is held. A failed exit cannot authorize
a completion receipt. Body failure releases the pin and evidence lease; if
bounded pin cleanup cannot finish, the pin continues to fence the owner.
Returned nested identity values are private copies. The handoff does not change
task authority, publication history, or the controller's `collectionVerified`
field. `FleetBuildEvidence` is defined in the collection module and remains
importable from the jobs module.

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

## Ordinary caller and project tools

`FleetBuildService` uses the ordinary controller submit, status and cancel APIs.
`FleetBuildOwner` retains exact caller intent through the existing journal.
`FleetBuildDispatcher` accepts a registered context ID. Its optional MCP server
exposes three tools: `fleet_build_submit`, `fleet_build_status` and
`fleet_build_cancel`. These callers do not claim a worker run grant or create
an executor. See the [ordinary consumer architecture](architecture.md#ordinary-fleet-build-consumers)
for identity checks, durable retry, logging and protocol lifecycle rules.

The owner borrows its journal and client. The [worker journal](fleet-journal.md)
now serializes storage operations and close with a reentrant thread lock. It
preserves the process writer flock, fences uncertain writes, and offers copied
snapshots and short compound transactions. FleetBuildOwner reads a copied record
snapshot and checks writer health even when its intent was already saved. It
checks again before a cancellation POST. FleetWorker reads copied session state;
inspection results cannot change retained workspace ownership or event identity.
Its session record, event sequence read, and event append share one short journal
transaction. Provider and HTTP calls remain outside that transaction. Other
consumers must adopt these interfaces before they can share the same journal.

The storage lock does not establish the whole SDK/provider/pool borrower lifetime.
The explicit local controller profile uses
[`FleetWorkerRuntime`](../hephaestus/automation/fleet_worker_runtime.py) to own
the shared worker journal, SDK loop/client, provider, and pool. It uses a real
directory flock and the existing `journal_directory` durability helper before
external effects. See the [worker runtime contract](fleet-worker.md#local-controller-runtime).

## Registered pipeline builds

[`FleetBuildJobRunner`](../hephaestus/automation/fleet_build_jobs.py) is the
synchronous adapter for `BuildTestJob(fleet_context_id=...)`. Supply it through
`WorkerPool(fleet_build_runner=...)`, with a private `evidence_receipt_dir`.
The adapter constructs no SDK client, journal, event loop, scheduler or remote
executor. Missing runner or receipt capability is refused before HTTP and local
or immutable build preparation. Jobs without selection keep the existing path.

Each `FleetBuildJobContext` binds one existing owner to the canonical repository,
source path, exact submission, full parent assignment, original snapshot and two
exclusion capabilities. The owner exposes its immutable context ID and a copy
of its submission for checks before transport. The job must match the repository,
source, optional expected head and exact `just test-unit` argv. Immutable-source
and host-runner overrides are incompatible with this selection. The shared
snapshot verifier checks the actual archive and manifest before submission;
registration owns the opaque reference-to-artifact mapping.

One absolute job deadline starts before source-lease acquisition. SDK submit
and status calls run on the supplied owner's loop; collection runs on the pool
thread. `FleetBuildEvidence` supplies the trusted publisher's private reference
and full expected identity, including the execution owner's lease ID. An ordinary
admission record does not contain that lease and cannot replace this handoff.
The runner compares the actual terminal reference to the supplied private receipt
before collection. The pool retains source and evidence exclusion through
collection and pending receipt capture.

Only a trusted publisher result that passes the real collector can establish
job success. The collector must confirm the admitted identity, actual bytes,
successful outcome, disposal and `verified_current` source. A terminal controller
status is insufficient. The existing private result boundary does not authorize
remote HTTPS retrieval. Production Slurm/Pyxis attachment and the two AMD64 pool
qualification gates described above remain open.

## Completion receipts and eligibility

The pool captures the bounded collection in one logical pending receipt under
both leases. Its `ok` and `succeeded` fields are false. Resource exit must succeed
before the same receipt can be finalized. The complete collection stays in
`JobResult.value` and the receipt, including identity, private reference, artifact
descriptors and source observation. The unit-only recipe leaves
`tested_patch_sha256` null; it cannot replace canonical pre-PR CI by hashing a
different patch after source release.

Success additionally requires the completion's private live acknowledgment.
`JobResult.fleet_receipt` is optional for legacy jobs, remains in process, and
must not enter journal, HTTP or UI projections. Its finalizer issues it only
after exact receipt bytes and file/directory durability are acknowledged and
the original deadline and shutdown signal are checked again. It retains no lease
or task-admission authority. The
[`read_fleet_build_receipt`](../hephaestus/automation/fleet_build_receipts.py)
reader requires that acknowledgment, a matching successful uninterrupted result,
and the caller's expected collection identity. It independently reads and hashes
the actual private candidate through the existing bounded no-follow file reader.

A write can succeed and then report failure. If the corrective failed-state
write also fails, the candidate may still look finalized. That error remains
explicit and no live acknowledgment is returned; the file alone cannot establish
Fleet success. Corrective storage has a separate two-second budget that cannot
extend execution or send controller requests. Pending, failed, interrupted,
changed or unacknowledged candidates remain ineligible. There is no marker whose
absence could imply success.

Restart loses the live acknowledgment. Retained files support explicit trusted
reconciliation and recollection; they cannot automatically promote a restored
job result. `sourceCurrent` describes source when collection held its lease,
not its state after release. A complete result for changed source is retained as
historical failed work. Stage consumers must use the closed reader and match the
expected snapshot when evaluating evidence.

## Remaining runtime integration

Deadline expiry, shutdown and attachment detachment stop local observation only.
They do not authorize a remote cancellation POST. The bridge waits for the actual
owner task's completion callback, including cancellation before its first step;
proxy cancellation is insufficient. The supplied lifetime must keep the loop
alive through all pool completions and journal access. Loop failure or an
unresponsive runtime still requires its process owner to preserve uncertainty
and enforce closure. Explicit remote cancellation stays a separate durable
authorized owner operation.

`WorkerPool.wait_for_exit()` seals new submissions through completion callback
registration, then waits for accepted jobs and their callbacks to exit. It keeps
queued work and borrowed resources available. The runtime owner must keep the
SDK loop and journal open until this barrier returns, then close them in order.
Existing `shutdown()` retains its cancellation and process termination behavior.

The Fleet CLI now constructs the concrete runtime for its explicit local
controller profile. Normal and failed-loop cleanup keep the SDK client until
the actual pool completion barrier returns. A shared loss signal fences both
build scheduling and queued task creation. The socket checks runtime health
again after reading a frame and before dispatch. An unrecoverable runtime keeps
its local uncertain marker and journal until the exact CLI process exits.

The factory starts with an empty registry. Its trusted in-process
`register_build()` operation accepts later contexts on the worker control
thread. It compares each context with the retained session, copies its identity,
and constructs an owner on the existing SDK loop with the same client and
journal. It requires source and result capabilities supplied by the execution
owner. Existing runner constructor mappings keep their original deferred
validation behavior; later registrations enforce the stronger owner binding.
Local context validation cannot authorize work outside Agamemnon admission.

`submit_build()` bounds outstanding jobs and unobserved completions together by
worker capacity. It permits one outstanding job per context. `take_completion()`
releases capacity only after observing the actual queue result. The runtime
uses the pool's completion notifier; saturation preserves uncertainty instead
of treating a lost completion as delivered evidence. `retire_build()` requires
that no job is outstanding. Failed and retired identities remain fenced, with
a lifetime limit of 4096 identities. Neither operation deletes build intents or
converts an observed result into an approved task outcome.

No CLI socket operation or implementation stage activates this registry.
Production source quiescence must cover all execution writers; drain, idle
state, or a generic file lock does not supply it. The local supervisor-to-reader
handoff supplies trusted expected-lease evidence under its actual result lock.
Snapshot transfer, concrete allocation and remote result transport, structured
stage consumption, and complete recovery qualification remain required. The
controlled two-build test drives the real CLI factory through an in-process
serve callback with fixture admission and source/result capabilities. Actual
HTTP, private files and harmless recipes do not qualify Slurm, Pyxis, canonical
CI, an operational allocation or 108 active agents.
