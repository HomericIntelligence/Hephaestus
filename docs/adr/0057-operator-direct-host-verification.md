# ADR-0057: Operator-direct host verification without a configured Linux backend

- Status: Accepted
- Date: 2026-10-14
- Tracks: operator directive of 2026-10-14; supersedes part of ADR-0056 (draft)

## Context

ADR-0056 (draft) states that missing providers block execution. On Linux, the
only reviewed quota backend is the Pyxis/Enroot boundary. A host without its
image, authority, or bounded filesystem receives `host_verification_quota_unavailable`
on every capability-dependent local lane. In production this blocked 42 of 68
items at writer rebase during the 2026-10-13/14 loop run, and no issue
implementation could proceed on that host.

The operator directed on 2026-10-14: "Delete any tests that require
pyxis/enroot. Hephaestus does not require this." The directive accepts that
fixed host checks for the operator's own source run without a container
boundary when no backend is configured.

## Decision

When no quota backend is configured, these lanes run the fixed check directly
from a disposable archive of the proven commit:

- the writer-rebase lanes (initial and continued), which keep their structural
  validation and carry no capability receipt for that run;
- the immutable build/test lane used by repository validation, which runs with
  a scrubbed environment (no inherited credentials), bounded wall time, a
  bounded writable scratch, and process-group termination on overrun.

"Not configured" follows one canonical rule for every consumer, the Linux rule
`select_quota_backend` already applies: the pinned image digest, the trust
authority, and the writable quota root are not all present. A default image
value without trust inputs is therefore unconfigured, so the production wiring
reaches the direct path.

The direct path does not deny network access or host file access. Candidate
code executes with the operator's reach. The operator accepts this posture by
not configuring a backend. Source sealing and post-run checkout verification
are kept.

Two lanes execute code from arbitrary pull requests and stay fail closed when
the strict sandbox is not configured:

- the PR-review host-verification lane, which also requires a configured
  Pyxis backend for its durable receipt contract;
- the Comet repository-validation local lane, which keeps the behavior that
  macOS has through its native sandbox and Linux has only through Pyxis.

Changing either lane needs a reviewed receipt contract for direct-boundary
evidence and a separate operator decision.

A configured rear-end is unchanged and stays fail closed on every probe,
image, authority, runtime, or filesystem failure. macOS keeps its native
sandbox boundary. Other platforms stay fail closed.

Tests that required pyxis/enroot are deleted: the opt-in end-to-end acceptance
lane, its acceptance-control unit test, the justfile recipe, and the conftest
requirement option. Unit coverage of the optional Pyxis backend is kept.

## Alternatives considered

- Keep ADR-0056 blocking on every host without a provider. Rejected: the
  operator directed that Hephaestus does not require pyxis/enroot, and the
  block made every local lane unusable on an unconfigured Linux host.
- Extend the direct boundary to the PR-review host-verification lane in the
  same change. Rejected: that lane executes code from arbitrary pull requests,
  its durable receipt contract requires container metadata, and widening the
  threat posture needs a reviewed receipt contract and a separate operator
  decision.
- Treat only a completely absent image as unconfigured, or require only the
  trust inputs without the quota root. Rejected: the production wiring always
  supplies the default image value when no flag is given, so the first rule
  would keep the direct path unreachable, and both rules would diverge from
  the canonical predicate that `select_quota_backend` applies.
- Introduce a separate direct-runner module instead of reusing the worker
  helpers. Rejected: sealing, environment scrubbing, bounded scratch, and
  result mapping already exist beside the dispatch.

## Consequences

Issue-implementation loops run on an unconfigured Linux host without blocked
writer rebases. The durable missing-provider failure receipt now applies only
to the PR-review capability lane and to configured backends that fail. The
threat posture for the operator's own source moves from sandbox prevention to
scrubbed-environment execution with post-run source verification. Any future
extension of the direct boundary to arbitrary pull-request code requires a new
receipt contract, independent review, and explicit operator acceptance.
