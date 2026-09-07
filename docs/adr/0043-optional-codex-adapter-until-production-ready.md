# ADR-0043: Optional Codex adapter until production readiness

- Status: Accepted
- Date: 2026-09-07
- Tracks: #3059, #3062
- Supersedes: ADR-0042 mandatory adapter selection

## Context

ADR-0042 requires an external adapter for Codex implementation jobs. The base
contract shipped without a working production adapter. Stock installations
can plan and review issues but cannot implement them with Codex.

The operator has approved a temporary relaxation until a complete adapter is
available. Issue #3062 owns that implementation and its readiness evidence.

## Decision

When no adapter or deployment-lock option is supplied, Codex implementation
uses the existing native direct runner. Keep its native sandbox, worktree
checks, session handling, and host-owned publication checks. Freeze the
approved plan scope before implementation and validate publication paths.

If any adapter option is supplied, use the adapter path. Require the complete
adapter selection and valid deployment evidence. A partial selection, failed
admission, or adapter execution failure must not cause direct fallback.

The temporary direct path uses shared host state in `HOME/.codex`. It
does not provide the private VM state, provider-only transport, or verified
descendant cleanup described in ADR-0042. The session-contamination concern in
issue #2887 remains open. Do not describe the direct runner as equivalent to
the adapter boundary.

Retain the adapter implementation, validation, and tests. Do not change Pi
admission or other provider policies. Planning, review, and learning keep
their existing behavior.

Before mandatory selection can return, issue #3062 must deliver a working
release, repeatable installation, supported-host integration evidence, and a
complete end-to-end implementation workflow. Evaluate Podman as one possible
backend. Record any change to the full isolation contract in an accepted ADR.
Re-enablement requires a separate reviewed change with migration instructions.
Issue closure alone does not activate the gate.

## Alternatives considered

- Keep the mandatory gate. Rejected because no production adapter is available.
- Remove all adapter support. Rejected because selected deployments must retain
  their validation and the future implementation can use the existing contract.
- Fall back after adapter failure. Rejected because it would silently weaken an
  explicitly selected boundary.
- Build a second temporary isolation backend. Rejected because the immediate
  change only restores the existing native runner.

## Consequences

Stock installations can implement issues with Codex again. The direct path has
weaker isolation, and operators must account for its access to configured
provider state. Explicit adapter deployments remain strict.

Rollback can restore the mandatory selection check without changing stored
data. It will again block stock Codex implementation jobs. A future mandatory
gate must not be released before the production-readiness criteria above pass.
