# ADR-0029: Explicit Pi isolation-adapter bootstrap

- Status: Accepted
<<<<<<< HEAD
- Date: 2026-08-19
- Tracks: #2791, #2738
=======
- Date: 2026-08-10
- Tracks: #2519, #2738
>>>>>>> 13d70500 (fix(agents): bootstrap external Pi isolation adapters)
- Extends: ADR-0019, ADR-0020

## Context

Pi's model-visible tool flags do not provide the operating-system filesystem
and network isolation required by the execution-policy matrix. Stage 5
therefore made normal Pi automation depend on a reviewed, host-owned
`PiIsolationAdapter` and kept stock installations fail-closed.

The original registration function works only when an embedding application
imports Hephaestus and registers an adapter in the same Python process. The
Stage 6 conformance commands start installed console scripts as fresh
processes, so an installed external broker had no explicit way to register
itself before `resolve_agent("pi")` enforced the adapter gate.

## Decision

External broker packages may publish a zero-argument adapter factory in the
`hephaestus.pi_isolation_adapters` Python entry-point group. A fresh process
loads exactly one named factory only when the operator explicitly sets
`HEPH_PI_ISOLATION_ADAPTER` to that public entry-point name.

Hephaestus does not bundle a broker, enumerate and auto-select installed
brokers, or fall back to direct Pi execution. Missing, duplicate, unloadable,
or structurally invalid selected entry points raise
`PiIsolationUnavailableError` before a provider process starts. Initialization
diagnostics are not exposed because an external broker may contain private
provider configuration.

The existing in-process `register_pi_isolation_adapter()` seam remains
available to embedding applications. Both bootstrap paths end at the same
`PiIsolationAdapter.invoke()` boundary; the external implementation remains
responsible for enforcing every filesystem and network grant in the resolved
`ExecutionPolicy`. Hephaestus supplies the complete minimized child environment
at that boundary. This is the only supported channel for an operator-selected
`PI_CODING_AGENT_DIR`; adapters must not inherit the ambient host environment.

## Alternatives considered

- **Bundle a Bubblewrap, container, or namespace adapter.** Rejected because
  Hephaestus cannot currently enforce the provider-relay and constrained-web
  contracts portably, and a partial adapter would weaken the Stage 5 boundary.
- **Auto-load the only installed adapter.** Rejected because package
  installation must not silently activate executable security-boundary code.
- **Accept a module path or command from an environment variable.** Rejected
  because a named packaging entry point provides an enumerable installation
  contract and avoids inventing a second import or subprocess protocol.
- **Require a custom Python wrapper for every console command.** Rejected
  because it does not satisfy the documented installed-console workflow and
  duplicates command dispatch outside Hephaestus.

## Consequences

- A host integration can bootstrap the same reviewed adapter across every
  Hephaestus console entry point without modifying pipeline code.
- Stock installations and installed-but-unselected broker packages remain
  fail-closed.
- The adapter must launch the supplied non-interactive command with the
  supplied child environment so the exact operator-local Pi profile and model
  selection survive external isolation without forwarding host credentials.
- Broker implementation, OS-level enforcement, adversarial validation, and
  deployment remain a separate deliverable tracked by #2738; this decision
  adds no claim that Stage 6 evidence is complete.
- The entry-point group, environment variable, zero-argument factory, and
  `PiIsolationAdapter` protocol form a public integration contract and require
  compatibility review before future changes.
