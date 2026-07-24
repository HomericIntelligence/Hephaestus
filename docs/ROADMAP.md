# Hephaestus Roadmap

## Vision

Hephaestus is the foundational utilities and tooling repository of the HomericIntelligence ecosystem, providing standardized components that support development across all other projects. We prioritize modularity, reliability, and consistency across a diverse set of cross-cutting concerns: configuration management, logging, GitHub automation, and agent coordination.

## Current Focus (Q3 2026)

Remediation of the strict 2026-05-28 audit findings (`audit-finding` issues) continues, now alongside hardening of the queue-based automation pipeline delivered under Epic #1809 and the fail-closed auto-merge bootstrap (#2054/#2055). This ongoing work spans:

1. **Audit Remediation** — Addressing the open `audit-finding` issues across the 15 audit dimensions. Focus areas include documentation currency, automation module test coverage, fixing f-string logging anti-patterns, and continued hardening of the 3-stage review-PR pipeline (collapsed from the prior 6-phase design in #677/#679).

2. **Automation Pipeline Hardening** — Stabilizing the queue-based
   plan → implement → review pipeline (Epic #1809): drive-green loop
   behavior, orphan-PR recovery, epic/skip-tag scoping, and restoring
   the reviewed-head interlock while `merge_wait` safely stands by on a
   confirmed-unarmed PR pending the separately reviewed #2419 merge path.

3. **CLI Tool Coverage Expansion** — Expanding the CLI entry point test suite from 13 of 47 declared tools to full coverage, ensuring all command-line interfaces are properly validated.

4. **Test Coverage Hardening** — Bringing 12 excluded automation modules into coverage measurement with mocked unit tests for core orchestration logic.

5. **Security & Dependency Management** — Hard-blocking pip-audit failures in CI and resolving dependency consistency issues across pyproject.toml and pyproject.toml.

## Near-term (Next 1-2 Quarters)

Assuming audit remediation is complete:

1. **Multi-platform CI Support** — Extend GitHub Actions test matrix to include macOS and Windows alongside Ubuntu, addressing the gap between pyproject.toml multi-platform claims and CI reality (#321 context).

2. **Cross-Repository Coverage** — Expand hephaestus utility adoption across other HomericIntelligence projects. Standardize configuration loading, logging setup, and subprocess execution patterns.

3. **API Surface Documentation** — ✅ Auto-generated API reference is now published to
   GitHub Pages on each release (pdoc). Remaining: ensure every public function carries a
   complete docstring, including stable subpackage surfaces and a full CLI reference.

4. **Observability and Health Checks** — Add structured health reporting for long-running components (e.g., NATSSubscriberThread), supporting the broader Argus (observability) initiative.

## Long-term (4+ Quarters Out)

Conservative, directional items:

1. **Agent Coordination Framework** — Explore deeper integration with Myrmidons for agent swarm coordination patterns, building on existing entry points for orchestration.

2. **Benchmark Suite Expansion** — Enhance the benchmark comparison utilities to support cross-project performance tracking and regression detection.

3. **Configuration Ecosystem** — Investigate dynamic configuration patterns (Proteus integration) and configuration composition across multiple environments.

## How We Plan

Hephaestus uses an Epic-and-children issue pattern for project planning. Major initiatives are tracked as Epic issues (labeled `epic`), with breakdown into concrete child issues tagged by audit section and severity.

**Exemplar:** Epic #310 (Strict audit 2026-04-28, now closed) contained 29 child issues spanning all 15 audit dimensions, with clear scoping and evidence-based requirements.

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
whenever a release is cut. Any Epic being opened or closed, or a shift in
priorities, is also a valid trigger to refresh it between releases.

**Responsibility.** The maintainer cutting the release owns the roadmap review
for that cycle: confirming the "Current Focus" section still reflects open
Epics and updating the "Last updated" date below. In this solo/small-team repo
that is the release maintainer; there is no separate roadmap committee.

**How to propose changes.** Open an issue that references this document (or a
PR editing it directly). The roadmap is refreshed to reflect current focus
areas as Epics are created or priorities shift.

Last updated: 2026-07-18
