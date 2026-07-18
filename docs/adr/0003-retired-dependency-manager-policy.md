# ADR-0003: Retired dependency-manager policy

- Status: Retired
- Date: 2026-07-16

## Context

The project previously carried more than one dependency-environment policy.
That policy is retired because it made local and automated commands ambiguous.

## Decision

The project uses the uv environment and lockfile policy recorded in ADR-0008.
No alternate environment manager or lockfile is supported.

## Alternatives considered

- **Retain multiple environment policies.** Rejected: users and automation can
  select different dependency graphs.

## Consequences

- All active development, automation, and CI commands use uv.
- This retained ADR number exists only to preserve the historical ADR sequence.
