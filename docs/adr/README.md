# Architecture Decision Records

This directory records significant architectural decisions for Hephaestus
using the [Nygard ADR format](https://github.com/joelparkerhenderson/architecture-decision-record).
Each ADR is immutable once Accepted; supersede rather than edit.

To add an ADR: take the next zero-padded number, copy the section skeleton from
any existing ADR (`# ADR-NNNN: …`, `- Status:`, `- Date:`, `- Tracks:`,
`## Context`, `## Decision`, `## Alternatives considered`, `## Consequences`),
and add a row to the table below. The structural guard
`tests/unit/docs/test_adr_records.py` keeps every ADR well-formed, contiguously
numbered, and listed here.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-automation-library-boundary.md) | hephaestus.automation is an opt-in product layer | Accepted |
| [0002](0002-pep562-lazy-imports.md) | PEP 562 lazy imports back the library/product boundary | Accepted |
| [0003](0003-dependabot-renovate-split.md) | Dependabot owns pip+actions; Renovate owns pixi/conda | Accepted (historical; superseded by 0008) |
| [0004](0004-single-aggregator-required-checks.md) | Single aggregated required-checks gate | Accepted |
| [0005](0005-multi-agent-runtime-abstraction.md) | Multi-agent (Claude/Codex/Pi) runtime abstraction | Accepted |
| [0006](0006-queue-based-in-process-automation-pipeline.md) | Queue-based in-process automation pipeline | Accepted |
| [0007](0007-dual-surface-required-checks.md) | Dual-surface required checks | Accepted |
| [0008](0008-uv-only-development-environment.md) | uv is the sole development environment manager | Accepted |
| [0009](0009-head-bound-strict-review-merge-gate.md) | Head-bound strict review controls queue-owned merge eligibility | Accepted (historical; superseded by 0012) |
| [0010](0010-trusted-strict-review-proof.md) | Trusted strict-review proof context | Superseded by 0012 (historical) |
| [0011](0011-mcp-integration-posture.md) | MCP is optional project-scoped agent tooling | Accepted |
| [0012](0012-loop-owned-pr-review-approval.md) | Loop-owned Athena PR-review approval | Accepted |
| [0013](0013-backup-and-disaster-recovery-policy.md) | Tiered backup and disaster-recovery policy | Accepted |
| [0014](0014-agent-contract-canonical-location.md) | AGENTS.md is the canonical agent contract | Accepted |
