# ADR-0014: Fixture-based workflow validator tests

- Status: Accepted
- Date: 2026-07-20
- Tracks: #2330

## Context

ADR-0004 recorded that the aggregate required-checks gate's `needs:` membership
was guarded by `tests/unit/ci/test_required_checks_gate.py`. That guard was
removed when the repository stopped maintaining live-tree workflow-content
snapshots. Rewriting ADR-0004 to describe that removal would erase its accepted
historical decision, and YAML or schema parsing alone cannot prove that every
gating job fans into `required-checks-gate`.

The workflow-validator library remains a public, fixture-testable interface:
it inventories workflow files, validates checkout ordering, and exposes two CLI
entry points. Those behavior tests must remain independent of this checkout's
workflow inventory and workflow contents.

## Decision

1. Preserve ADR-0004 verbatim as the accepted historical record. ADR-0007
   remains the successor for the required-check policy itself.
2. Do not replace the removed aggregate-gate membership test with a claim that
   generic YAML or schema tooling validates complete fan-in. Changes to the
   aggregate workflow require direct review and the live audit in
   `docs/ci/required-checks.md`.
3. Retain fixture-based tests for `hephaestus.ci.workflows`, including helper
   boundaries, error handling, and both registered public CLI entry points.
   Remove only tests that snapshot or assert the repository's current workflow
   files, their contents, or their existence.

## Alternatives considered

- **Rewrite ADR-0004.** Rejected: accepted ADRs are immutable; a successor
  records the changed testing decision without changing history.
- **Claim YAML/schema validation proves complete gate fan-in.** Rejected:
  syntax and schema validation cannot detect a gating job omitted from a
  `needs:` list.
- **Remove all workflow-validator tests with live snapshots.** Rejected: that
  discards independent product behavior coverage unrelated to repository
  workflow contents.

## Consequences

- Workflow-validator tests use temporary fixture repositories and retain their
  public CLI coverage without coupling to `.github/workflows/` in this checkout.
- Required-check fan-in changes receive direct review and a live required-check
  audit; the repository makes no automated completeness claim that it cannot
  verify.
- Future changes to the test strategy are documented in successor ADRs rather
  than edits to accepted ADRs.
