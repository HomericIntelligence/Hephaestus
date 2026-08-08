# ADR-0023: Pi package bootstrap and preflight

- Status: Accepted
- Date: 2026-08-08
- Tracks: #2516

## Context

ADR-0019 separates Pi's base runtime from capabilities supplied by packages.
Normal automation must not load unverified extensions merely to discover whether
the required package set is present. Operators and CI also need one reproducible
way to install the exact compatible CLI, Athena skills, delegation, and web
capabilities without creating independent pin authorities.

## Decision

`hephaestus/agents/pi_package_catalog.json` is the distributable authority for
the exact Pi npm identity, Athena commit, and companion npm versions. The
`hephaestus-install-pi-plugins` command derives argument vectors from that file,
defaults to global scope plus `--no-approve`, requires explicit confirmation for
mutation, and provides dry-run, JSON, scope, approval, and timeout controls.

Preflight resolves one absolute `pi` executable, binds it to the owning npm
manifest and exact version, validates settings and installed roots before
extension execution, then uses a nonce-correlated RPC probe. Commands and tools
count only when Pi's canonical `sourceInfo` binds them to the statically verified
package root and scope. Timeouts terminate the process group and child output is
bounded. Partial installs remain in place for an idempotent rerun; automatic
rollback and `pi update` are not part of the ownership contract.

Package readiness is necessary but not sufficient for automation. Runtime entry
points report the installer on preflight failure and retain #2518's independent
lifecycle and role-scoped tool denial after preflight succeeds.

## Alternatives considered

- Keep literals in CI, shell, and documentation. Rejected because compatible
  pins would drift across consumers.
- Discover capabilities by loading packages before inventory validation.
  Rejected because unverified extensions execute with host privileges.
- Treat package readiness as full Pi admission. Rejected because #2518 owns the
  separate per-stage lifecycle and tool-scope contract.
- Roll back successful packages after a later failure. Rejected because Pi's
  native package manager is reconciliation-based and rerunning exact pins is
  simpler and observable.

## Consequences

Pin changes require a reviewed catalog update. Athena commit changes rerun the
native-package acceptance workflow; CLI or companion-package changes rerun the
gated live package smoke. The catalog and RPC probe must ship in wheels and
sdists. Operators receive actionable failure states without weakening the
remaining automation admission gate.
