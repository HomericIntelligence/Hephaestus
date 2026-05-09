# ProjectHephaestus Documentation

Welcome to the official documentation for ProjectHephaestus.

## Overview

ProjectHephaestus is the shared utilities and tooling library for the HomericIntelligence ecosystem.

## Subpackages

- **hephaestus.utils** — General utility functions (slugify, retry, subprocess helpers)
- **hephaestus.config** — Configuration loading and management (YAML, JSON, env vars)
- **hephaestus.io** — File I/O utilities (read, write, safe_write, load/save data)
- **hephaestus.logging** — Enhanced logging (ContextLogger, setup_logging)
- **hephaestus.cli** — CLI argument parsing and output formatting
- **hephaestus.system** — System information collection
- **hephaestus.github** — GitHub automation (PR merging)
- **hephaestus.datasets** — Dataset downloading utilities
- **hephaestus.markdown** — Markdown linting and link fixing
- **hephaestus.benchmarks** — Benchmark comparison and regression detection
- **hephaestus.version** — Version management utilities
- **hephaestus.validation** — README and config validation

## API Reference

Auto-generated API documentation can be produced with [pdoc](https://pdoc.dev/):

```bash
just docs        # outputs to docs/api/
```

The generated `docs/api/` directory is git-ignored; run the command locally to browse
full function signatures, docstrings, and type annotations for all 37+ CLI entry points.

## Setup

See the [README](../README.md) for installation and development setup instructions.

- [Plugin Installation Guide](plugin-installation.md) — Install the Claude Code plugin and enable skills in your project

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines.
