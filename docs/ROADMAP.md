# Hephaestus Roadmap

## Vision

Hephaestus is the foundational utilities and tooling repository of the HomericIntelligence ecosystem, providing standardized components that support development across all other projects. We prioritize modularity, reliability, and consistency across a diverse set of cross-cutting concerns: configuration management, logging, GitHub automation, and agent coordination.

## Current Focus (Q3 2026)

**Owner:** The maintainer cutting the release.

**Trigger:** The pre-release checklist, an epic state change, or a priority change.

**Maintained source:** [Open GitHub epics](https://github.com/HomericIntelligence/Hephaestus/issues?q=is%3Aopen%20label%3Aepic),
open `audit-finding` issues, and the [pre-release checklist](RELEASING.md#pre-release-checklist).

The authoritative current backlog is the set of open GitHub epics and
`audit-finding` issues. This document records durable direction and planning
policy; it does not mirror issue open/closed state, issue counts, source
metrics, or calendar-quarter snapshots.

Current themes are:

1. **Automation stabilization** — improve orchestration reliability,
   observability, testability, and worktree safety.
2. **Cross-platform support** — align tested platforms with supported platforms.
3. **Public API documentation** — keep stable package and CLI surfaces complete
   and machine-validated.
4. **Security and dependency management** — keep vulnerability, secret, and
   dependency-consistency gates blocking and maintainable.

## Longer-term direction

- Expand cross-repository adoption of shared utilities.
- Improve benchmark and regression-detection tooling.
- Explore configuration composition and agent-coordination integrations when a
  concrete consumer requires them.

## How We Plan

Hephaestus uses an Epic-and-children issue pattern for project planning. Major initiatives are tracked as Epic issues (labeled `epic`), with breakdown into concrete child issues tagged by audit section and severity.

We also capture session learnings in Mnemosyne via the `/learn` skill, preserving team knowledge about patterns, anti-patterns, and decisions across the ecosystem.

## Updating This Roadmap

**Cadence — release-driven, not date-driven.** A "release cycle" is not a
calendar interval; it is each `vX.Y.Z` release cut through the **Auto Tag
Release** workflow (see [RELEASING.md](RELEASING.md)). Because that workflow is
triggered manually when a batch of features/fixes is ready — not on a fixed
schedule — releases (and therefore roadmap reviews) are **feature/fix-driven,
not date-driven**. Cadence in practice tracks release frequency rather than a
fixed monthly rhythm.

**Trigger.** The roadmap is reviewed as part of the pre-release checklist,
whenever a release is cut. Opening or closing an epic, or a shift in priorities,
is also an update trigger.

**Responsibility.** The maintainer cutting a release owns the roadmap review
for that cycle: compare these themes with the open epics and update the
document when direction changes.

**How to propose changes.** Open an issue that references this document (or a
PR editing it directly). Current execution status remains in GitHub, not in a
manual status inventory.

Last updated: 2026-07-20
