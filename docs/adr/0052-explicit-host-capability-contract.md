# ADR-0052: Explicit host capability contract

- Status: Accepted
- Date: 2026-09-15
- Tracks: #2903

## Context

The pipeline runs source checks and Git write operations on host resources.
The prior implementation selected some resources from process state. It also
used a create-only disk-image probe. These paths did not prove that the host
could attach and detach a volume. They did not produce a durable result that
identified the repository, checkout, source commit, process boundary, device,
phase, and purpose.

A failed runner setup is not a source review result. It must not cause an
implementation verdict or a remediation request. A rebase must also use the
same host boundary before it publishes a changed commit.

## Decision

The coordinator supplies one explicit `WorkerCapabilities` object to each
worker pool. This object supplies the quota backend, process-local preflight
cache, durable receipt-store factory, execution-boundary identity, and Git
signing provider. Production Git write operations use only the injected
signing provider.

A stage creates a strict capability request for one repository, issue, pull
request, repository root, checkout, expected source commit, phase, purpose,
and request identity. The worker adds the canonical root, filesystem device,
execution boundary, and observed source commit. It writes the result below
`build/.issue_implementer/host-capability-receipts`. Receipt directories use
mode `0700`. Receipt files and the directory lock use mode `0600`. Writes use
no-follow descriptor walks, an exclusive lock, atomic replacement, and strict
readback validation.

On macOS, the quota backend creates, attaches, validates, and detaches a disk
image. Scratch data and Pi smoke logs use the same backend with fixed purpose
paths. A failed create or attach removes partial state. A successful operation
removes its request directory. A detach failure keeps the request directory
and records its path for operator recovery. Symlinks and unexpected path types
fail closed.

The cache key includes the process, execution boundary, canonical repository
root, device, capability, and backend. A cache hit creates a new receipt for
the current target and refers to the original probe receipt.

PR review performs this preflight after it binds the detached source head and
before it reads existing threads, starts source checks, or starts review
analysis. A runner failure writes one bounded diagnostic and stops without a
GO label, a NO-GO label, or an implementation remediation request. Rebase
validation uses the same request and receipt contract for the rebased commit.
It completes structural and semantic validation before it publishes the new
head.

Linux keeps its reviewed Pyxis boundary for source execution. Other systems
stop with a typed unsupported-boundary result until the project adds a
reviewed isolation backend.

## Alternatives considered

A create-only probe cannot prove attach or detach behavior. A process-global
boolean does not identify a source target or execution boundary. Ambient Git
configuration gives tests and workers an implicit signing dependency. These
options do not give the pipeline a complete, durable capability result.

## Consequences

Workers have an explicit host dependency and tests can supply deterministic
fake providers. Host restrictions stop work before review verdict changes.
Operators can inspect strict receipts and retained detach-failure directories.
The pipeline does more host setup before each new process or boundary uses a
repository device.
