# Hephaestus Documentation

Welcome to the official documentation for Hephaestus.

## Overview

Hephaestus is the shared utilities and tooling library for the HomericIntelligence ecosystem.

## Documentation maintenance

Normative summaries state durable contracts. Do not copy temporary issue state,
calendar snapshots, or source/test-size metrics into them. An exact changing
value belongs in documentation only when the maintained source and validation
mechanism are named.

| Claim class | Maintained source | Owner / update trigger | Enforcement |
| --- | --- | --- | --- |
| Package and CLI inventories | `pyproject.toml`, package directories, and exported `__all__` values | Contributor changing the inventory; update in the same PR | CLI, API-table, and subpackage-tree validators |
| Enabled agent skills | `.claude/settings.json` and the installed plugin manifests | Contributor changing plugin enablement | Review the runtime-provided skill catalog; do not mirror a count |
| Pipeline topology and routing | `docs/AUTOMATION_LOOP_ARCHITECTURE.md` plus `hephaestus.automation.pipeline` | Automation maintainer; update with any stage or route change | Pipeline and documentation regression tests |
| Merge and required-check policy | `docs/ci/required-checks.md`, workflow definitions, and live GitHub protection | Maintainer changing either policy surface | Local policy tests plus the runbook's read-only live audit |
| Roadmap priorities | `docs/ROADMAP.md`, open GitHub epics, and audit-finding issues | Release maintainer; review before every release and on priority changes | Pre-release checklist and roadmap-cadence test |
| Released version and migration state | Immutable `vX.Y.Z` tags and `docs/MIGRATION.md` | Release maintainer before tagging | `tests/unit/docs/test_version_currency.py` |

ADRs and release notes may retain dates, issue numbers, and measured values as
historical evidence; they are not sources for current operating state.

See [Documentation Maintenance](documentation-maintenance.md) for the full
living-document scope and the source, ownership, and validation contracts.

## Subpackages

- **hephaestus.agents** — Agent frontmatter, loader, runtime, stats
- **hephaestus.automation** — Issue planning / implementation / PR review pipeline
- **hephaestus.benchmarks** — Benchmark comparison and regression detection
- **hephaestus.ci** — CI helpers (precommit, workflows, docker timing)
- **hephaestus.cli** — CLI argument parsing and output formatting
- **hephaestus.config** — Configuration loading and management (YAML, JSON, env vars)
- **hephaestus.datasets** — Dataset downloading utilities
- **hephaestus.discovery** — Discovery of agents, skills, and code blocks
- **hephaestus.forensics** — Coredump capture and gdb post-mortem runner
- **hephaestus.github** — GitHub automation (PR merging, fleet sync, tidy, stats, rate limit)
- **hephaestus.io** — File I/O utilities (read, write, safe_write, load/save data)
- **hephaestus.logging** — Enhanced logging (ContextLogger, setup_logging)
- **hephaestus.markdown** — Markdown linting, link fixing, anchor validation
- **hephaestus.nats** — NATS JetStream subscriber for event-driven workflows
- **hephaestus.resilience** — Circuit breaker + retry + subprocess resilience primitives
- **hephaestus.system** — System information collection
- **hephaestus.utils** — General utility functions (slugify, retry, subprocess and git helpers)
- **hephaestus.validation** — README, schema, and structural validation
- **hephaestus.version** — Version management (hatch-vcs + consistency checks)

## API Reference

Auto-generated API documentation is published to GitHub Pages on every release:

- **Browse online:** <https://homericintelligence.github.io/Hephaestus/>

The published reference covers full function signatures, docstrings, and type
annotations for documented first-level subpackages, excluding the
`hephaestus.automation` product layer, plus 51 CLI entry points, regenerated
from the released package via [pdoc](https://pdoc.dev/).

To build the same reference locally (output to the git-ignored `docs/api/`):

```bash
just docs        # outputs to docs/api/
```

## Setup

See the [README](../README.md) for installation and development setup instructions.

- [Audit Reviewer](audit-reviewer.md) — `hephaestus-audit-prs`: coordinator-pattern auditor for ALL open PRs (issue #994)
- [MCP Configuration](mcp.md) — Project-scoped `.mcp.json` and how to add a Model Context Protocol server
- [NATS JetStream Configuration](nats.md) - TLS defaults, certificate file paths, and local plaintext exceptions for `hephaestus.nats`
- [Operations Runbooks](runbooks/index.md) — Operator recovery procedures for the automation pipeline (loop crash, corrupted worktree, CI-driver stall, quota exhaustion)

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.
