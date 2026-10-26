# ADR-0060: Pi capability admission drops the sourceInfo provenance binding

- Status: Accepted
- Date: 2026-10-24
- Tracks: #3416

## Context

ADR-0023 made Pi capability admission depend on `sourceInfo` provenance:
commands and tools counted only when Pi's canonical `sourceInfo` bound them to
a statically verified package root and scope. Issue #3416 removes host-side
provenance and attestation layers because the agent contract says: do not add
a provenance or attestation layer for ordinary inputs; trust a value that came
from your own process and validate it at the boundary where it enters.

The preflight probe derives `sourceInfo` from settings files that the host
itself constructs. The isolated probe pins `HOME` to a temporary root that
exposes only inventory-verified package roots, so the re-check verified a
value the host wrote against a value the host verified. The layer was
self-referential: it could only fail when the already-enforced admission
checks had failed first.

## Decision

Remove the `_source_matches` check and its three call terms from
`verify_capability_inventory` in `hephaestus/agents/pi_plugins.py`. This
supersedes the ADR-0023 sentence "Commands and tools count only when Pi's
canonical `sourceInfo` binds them to the statically verified package root and
scope."

The trust anchors stay fail-closed and unchanged: CLI identity binding,
catalog-pinned settings, contained install roots, manifest identity, git HEAD
committed against the pin with a clean tree and tree digest, the isolated
probe environment, nonce-correlated RPC response parsing, and the
cardinality rejection that requires each locked command and active tool to
occur exactly once.

## Alternatives considered

- Keep the `sourceInfo` binding as defence in depth. Rejected because it adds
  no independent anchor: the checked value and the checker share one
  host-constructed source, and the agent contract forbids the layer.
- Replace it with a narrower name-only match. Rejected because a name-only
  version of the same self-referential data carries the same evidence value.

## Consequences

ADR-0023 is superseded in part: the capability `sourceInfo` provenance binding
is retired; its remaining decisions (preflight pipeline, settings validation,
RPC probe, bounded output, process-group timeouts) are unchanged.

A Pi built-in capability whose name collides with a catalog name now passes
where the removed check rejected it. The risk is confined to already-verified
package roots under exact pins, so the residual exposure is accepted with the
cardinality guard retained.

The ADR index records ADR-0023 as partially superseded by this record.
