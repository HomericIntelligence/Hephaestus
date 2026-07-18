# Hephaestus Documentation

Welcome to the official documentation for Hephaestus.

## Overview

Hephaestus is the shared utilities and tooling library for the HomericIntelligence ecosystem.

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
`hephaestus.automation` product layer, plus 48 CLI entry points, regenerated
from the released package via [pdoc](https://pdoc.dev/).

To build the same reference locally (output to the git-ignored `docs/api/`):

```bash
just docs        # outputs to docs/api/
```

## Setup

See the [README](../README.md) for installation and development setup instructions.

- [Audit Reviewer](audit-reviewer.md) — `hephaestus-audit-prs`: coordinator-pattern auditor for ALL open PRs (issue #994)
- [MCP Integration Posture](mcp.md) — Capability boundary, alternative integration contracts, and project-scoped `.mcp.json` change control
- [NATS JetStream Configuration](nats.md) - TLS defaults, certificate file paths, and local plaintext exceptions for `hephaestus.nats`
- [Operations Runbooks](runbooks/index.md) — Operator recovery procedures for the automation pipeline (loop crash, corrupted worktree, drive-green stall, quota exhaustion)
- [Performance Testing](performance-testing.md) — Bounded worker-pool load, capacity, latency, and sustained-concurrency testing

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.
