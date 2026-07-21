# Hephaestus

[![Test](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/test.yml/badge.svg)](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/test.yml)
[![Security](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/security.yml/badge.svg)](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/security.yml)
[![Release](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/release.yml/badge.svg)](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/release.yml)
[![Auto Tag](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/auto-tag.yml/badge.svg)](https://github.com/HomericIntelligence/Hephaestus/actions/workflows/auto-tag.yml)
[![PyPI](https://img.shields.io/pypi/v/HomericIntelligence-Hephaestus.svg)](https://pypi.org/project/HomericIntelligence-Hephaestus/)
[![Python](https://img.shields.io/pypi/pyversions/HomericIntelligence-Hephaestus.svg)](https://pypi.org/project/HomericIntelligence-Hephaestus/)
[![License: BSD-3-Clause](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)

Shared utilities and tooling for the HomericIntelligence ecosystem, powered by [uv](https://uv.sh) for environment management.

## Overview

Hephaestus provides standardized utility functions and tools that can be shared across all HomericIntelligence repositories. Following the principles in [AGENTS.md](AGENTS.md), this project emphasizes:

- **Modularity**: Well-defined, reusable components
- **Simplicity**: KISS (Keep It Simple, Stupid) principle
- **Consistency**: Standardized interfaces and patterns
- **Reliability**: Comprehensive testing and error handling

**Project Status:** See [docs/ROADMAP.md](docs/ROADMAP.md) for the public roadmap and current focus areas.

## Installation

### From PyPI

Hephaestus is published to PyPI under the ecosystem-branded distribution name **`HomericIntelligence-Hephaestus`**. The import name, however, is the short lowercase `hephaestus`:

```bash
pip install HomericIntelligence-Hephaestus
```

```python
import hephaestus
print(hephaestus.__version__)
```

> **Upgrading?** When moving across a major version, read the
> [migration guide](docs/MIGRATION.md) for required consumer changes.
>
> **Note on naming.** `pip install hephaestus` will **not** find this package — the bare name is unowned on PyPI. The `HomericIntelligence-<Name>` prefix is the deliberate naming convention shared across the HomericIntelligence ecosystem (Keystone, Odyssey, etc.) to avoid PyPI namespace collisions; the distribution is `HomericIntelligence-Hephaestus`. Wheel filenames are PEP 625 normalized to lowercase, so you will see `homericintelligence_hephaestus-<version>-py3-none-any.whl` on disk and in release assets.

### Optional dependencies

`pyproject.toml` defines several extras groups. `[all]` is a **runtime** aggregator
and intentionally excludes `[dev]` (which carries test/lint tooling such as
pytest, ruff, and mypy):

- `pip install HomericIntelligence-Hephaestus[all]` — installs all runtime
  extras: `automation`, `github`, `nats`, `toml`, `xml`, `schema`. Note that
  `automation` is the product layer (`hephaestus.automation`) and pulls in
  `pydantic`; see [ADR 0001](docs/adr/0001-automation-library-boundary.md).
- `uv sync` — installs the editable project plus its default development and
  automation dependency groups for contributors.
- `uv sync --all-groups --all-extras --locked` — installs the complete locked
  dependency surface used by CI dependency and license checks.
- Individual extras (e.g. `[github]`, `[schema]`) are available for users who
  only need one integration.

### Development setup

For local development, [install uv](https://uv.sh/install/) and
[`just`](https://just.systems/), then bootstrap the project (installs deps, the
editable package, and pre-commit hooks in one step):

```bash
just bootstrap
```

See [CONTRIBUTING.md → Development Setup](CONTRIBUTING.md#development-setup) for
the full workflow, including the manual fallback if you do not have `just`.

## Library vs product layer

Hephaestus ships two layers from one distribution:

- **Library** — `hephaestus.{utils, io, config, logging, cli, system,
  github, validation, resilience, markdown, ci, benchmarks, datasets,
  discovery, forensics, nats, version, agents}`. Loaded lazily by
  `import hephaestus`.
- **Product** — `hephaestus.automation`. Opt-in via
  `pip install HomericIntelligence-Hephaestus[automation]`. Implements
  the Claude/Codex automation pipeline (Planner, Implementer, CIDriver,
  reviewers, loop runner, curses TUI).

`import hephaestus` does **not** load `hephaestus.automation`, `curses`,
`fcntl`, or `pydantic`, and a base `pip install` no longer pulls `pydantic`
(it ships only in the `[automation]` extra). The boundary is enforced by
`tests/unit/validation/test_import_surface.py` and
`tests/unit/validation/test_automation_boundary.py`. See
[`docs/adr/0001-automation-library-boundary.md`](docs/adr/0001-automation-library-boundary.md).

## Directory Structure

```
Hephaestus/
├── pyproject.toml          # uv configuration
├── pyproject.toml     # Python package configuration
├── hephaestus/        # Main package
│   ├── __init__.py
│   ├── agents/        # Agent frontmatter + loader + runtime
│   ├── automation/    # Queue-based automation pipeline and scoped wrappers
│   ├── benchmarks/    # Benchmark comparison utilities
│   ├── ci/            # CI helpers (precommit, workflows, docker timing)
│   ├── cli/           # CLI helpers (argument parsing, output formatting)
│   ├── config/        # Configuration utilities (YAML, JSON, env vars)
│   ├── datasets/      # Dataset downloading utilities
│   ├── discovery/     # Discovery of agents, skills, and code blocks
│   ├── forensics/     # Coredump capture + gdb post-mortem runner
│   ├── github/        # GitHub automation (PR merging, fleet sync, tidy, stats)
│   ├── io/            # I/O utilities (read, write, safe_write, load/save data)
│   ├── logging/       # Logging utilities (ContextLogger, setup_logging)
│   ├── markdown/      # Markdown linting and link fixing
│   ├── nats/          # NATS JetStream subscriber (event-driven workflows)
│   ├── observability/ # Prometheus metrics, local health endpoint, and alert transitions
│   ├── prompts/       # Packaged Jinja templates and CLI-only override catalog
│   ├── resilience/    # Circuit breaker + retry + subprocess resilience primitives
│   ├── scripts_lib/   # Standalone consistency-check scripts (CLI table, version)
│   ├── system/        # System information collection
│   ├── utils/         # General utility functions (slugify, retry, subprocess, git helpers)
│   ├── validation/    # README, schema, and structural validation
│   └── version/       # Version management (hatch-vcs + consistency checks)
├── tests/             # Unit tests
├── docs/              # Documentation
├── scripts/           # Utility scripts
└── README.md          # This file
```

## Getting Started with uv

This project uses [uv](https://uv.sh) for environment management, which automatically handles dependencies and creates isolated environments.

> **Platform note:** uv supports this project's Python 3.10+ development
> environment on Linux, macOS, and Windows. The required GitHub Actions jobs
> currently run on Linux; POSIX-specific tests are marked to skip on native
> Windows. See [CONTRIBUTING.md#platform-support](CONTRIBUTING.md#platform-support).

### Prerequisites

Install uv by following the [official installation guide](https://uv.sh/install/).

### Setup Development Environment

Bootstrap the project in one step (see
[CONTRIBUTING.md → Development Setup](CONTRIBUTING.md#development-setup) for the
full workflow and the no-`just` fallback):

```bash
just bootstrap
```

### Running Tests

```bash
# Run all tests (unit + integration)
just test
uv run pytest

# Run only unit tests (coverage-gated in CI)
just test-unit
uv run pytest tests/unit

# Run only integration tests
just test-integration
uv run pytest tests/integration

# Run all tests except integration
uv run pytest -m "not integration"
```

All integration tests carry `pytest.mark.integration` (module-level `pytestmark`),
so marker-based selection is reliable.

### Development Commands

```bash
# Format code with ruff
just format
uv run ruff format hephaestus scripts tests

# Lint code with ruff
just lint
uv run ruff check hephaestus scripts tests
```

## Usage

### As a Package

After installing with uv:

```python
from hephaestus import slugify, human_readable_size, retry_with_backoff

# Convert text to URL-friendly slug
project_slug = slugify("My Project Name")
print(project_slug)  # Output: my-project-name

# Convert bytes to human readable size
size_str = human_readable_size(1048576)
print(size_str)  # Output: 1.0 MB
```

### Installing in Another Project

Hephaestus is published to PyPI as `homericintelligence-hephaestus`.
The wheel is pure-Python and installs on Linux, macOS, and Windows
(see `requires-python` in [`pyproject.toml`](pyproject.toml)). This is
the supported install path for non-Linux platforms.

**Using pip:**

```bash
pip install homericintelligence-hephaestus
```

**Using uv:**

Add to `pyproject.toml`:

```toml
[project]
dependencies = [
    "homericintelligence-hephaestus>=0.9,<1",
]
```

Then run `uv sync` to resolve the dependency.

After 1.0 ships, bump these constraints to `>=1.0,<2`.

**For local development (path dependency):**

```bash
uv add --editable ../Hephaestus
```

## Key Features

### General Utilities (`hephaestus.utils`)

- `slugify(text)`: Convert text to URL-friendly slug
- `retry_with_backoff(func)`: Decorator for exponential backoff retries
- `human_readable_size(bytes)`: Convert bytes to human readable format
- `flatten_dict(dict)`: Flatten nested dictionaries
- `run_subprocess(cmd)`: Execute shell commands with error handling
- `run_git(args, retries=None)`: Execute Git commands through the shared subprocess adapter with bounded timeout and network retry protection
- `get_setting(config, key_path)`: Get nested dict values with dot notation

### Configuration (`hephaestus.config`)

- `load_config(path)`: Load YAML or JSON configuration files
- `get_setting(config, key_path)`: Dot-notation config access
- `merge_configs(*configs)`: Deep-merge multiple configuration dicts
- `merge_with_env(config, prefix)`: Overlay environment variables onto config

#### Environment Variable Convention

`merge_with_env` maps environment variables to config keys using **double underscore (`__`) as the nesting delimiter**. Single underscores are preserved as part of the key name.

| Environment Variable | Config Key |
|---|---|
| `HEPHAESTUS_DATABASE__HOST` | `{"database": {"host": ...}}` |
| `HEPHAESTUS_MAX_CONNECTIONS` | `{"max_connections": ...}` |
| `HEPHAESTUS_DATABASE__MAX_RETRIES` | `{"database": {"max_retries": ...}}` |

Numeric strings are automatically converted to `int` or `float`. To also convert boolean-like strings (`true`/`false`/`yes`/`no`/`on`/`off`) to Python `bool`, pass `convert_bools=True`:

```python
from hephaestus.config.utils import merge_with_env

# HEPHAESTUS_DEBUG=true → {"debug": True} (not the string "true")
config = merge_with_env({}, convert_bools=True)
```

### I/O Utilities (`hephaestus.io`)

- `read_file(path)` / `write_file(path, content)`: Simple file I/O
- `load_data(path)` / `save_data(path, data)`: Structured data (JSON/YAML)

## CLI Commands

Run any command with `--help` to see full usage.

The package currently installs 49 console scripts from `[project.scripts]`.

### Automation

| Command | Description |
|---|---|
| `hephaestus-automation-loop` | Multi-repo queue-based automation pipeline using Claude Code or Codex (repo → planning → plan_review → implementation → pr_review → merge_wait → finished; restarted implementation-GO inputs re-enter `merge_wait` with their loop-owned approval label) |
| `hephaestus-plan-issues` | Bulk issue planning using Claude Code or Codex |
| `hephaestus-implement-issues` | Bulk issue implementation using Claude Code or Codex in parallel worktrees |
| `hephaestus-review-prs` | Read-only PR review automation using Claude Code or Codex in parallel worktrees |
| `hephaestus-agent-stage` | Run one Claude or Codex automation stage with prompt and skill context |
| `hephaestus-ensure-state-labels` | Idempotently provision `state:needs-plan` / `state:plan-no-go` / `state:plan-go` labels on one or more repos |
| `hephaestus-audit-prs` | Audit ALL open PRs in one coordinator agent invocation |
| `hephaestus-drive-prs-green` | Review open PRs and wait for their required branch-protection checks through the pr_review/merge_wait pipeline slice |

#### Private Pi provider setup

Pi uses operator-local provider configuration only. Do not commit Pi provider
config, endpoint URLs, hostnames, checkpoint names, model identifiers, or local
aliases. Configure the OpenAI-compatible provider in the local Pi config, set
`HEPH_PI_MODEL=<operator-local-alias>`, and see
[`docs/pi-private-provider.md`](docs/pi-private-provider.md) for the sanitized
setup and denylist guard.

#### Running the automation loop from a source checkout (macOS / Codex)

When `hephaestus-automation-loop` is not installed on `PATH` (fresh source
checkout) and Claude is not installed, invoke the loop through `uv` and pin
Codex as the agent:

```bash
# Prerequisites
command -v uv             # uv installed
command -v codex && codex login status   # Codex authenticated
command -v gh && gh auth status          # gh authenticated

# Title-scoped loop over open "nitpick" / "minor" issues
issues=$(
  gh issue list --state open --limit 500 --json number,title \
    --jq '.[] | select((.title | ascii_downcase) | test("(^|[^a-z0-9_])(nitpick|minor)([^a-z0-9_]|$)")) | .number' \
  | sort -n -u | paste -sd, -
)

test -n "$issues" \
  && uv run hephaestus-automation-loop --issues "$issues" --agent codex \
  || echo "No open nitpick/minor title issues found"
```

If the pre-loop `git fetch` is denied (e.g. macOS sandboxing returns
`error: cannot open .git/FETCH_HEAD: Operation not permitted`) the loop now
logs a WARNING and renders the trunk line as `[Repo] trunk=<sha> (stale)`
so the refresh failure is visible rather than silently treated as a clean
sync (#993).

### GitHub

| Command | Description |
|---|---|
| `hephaestus-fleet-sync` | Sync all PRs across the HomericIntelligence fleet |
| `hephaestus-gh` | Run `gh` through Hephaestus retry, circuit-breaker, and throttle handling |
| `hephaestus-github-stats` | GitHub contribution statistics via the `gh` CLI |
| `hephaestus-label-severity` | Reconcile the `severity:*` label for a GitHub issue from its issue-form Severity answer |
| `hephaestus-merge-prs` | Merge open PRs with successful CI/CD through the shared `gh` adapter |
| `hephaestus-tidy` | Single-repo gh-tidy wrapper with Myrmidon swarm for conflict resolution |

### System & Data

| Command | Description |
|---|---|
| `hephaestus-agent-stats` | Agent statistics aggregation and reporting |
| `hephaestus-download-dataset` | Dataset downloading utilities for Hephaestus |
| `hephaestus-system-info` | System information collection utilities for Hephaestus |

### Debugging & Forensics

| Command | Description |
|---|---|
| `hephaestus-coredump-handler` | Kernel pipe-mode `core_pattern` handler for capturing cores from containerized crashes |
| `hephaestus-run-under-gdb` | Run any command under `gdb -batch` to capture a real core before a runtime's own signal handler swallows the fault |

### Validation

| Command | Description |
|---|---|
| `hephaestus-audit-doc-policy` | Audit documentation command examples for policy violations |
| `hephaestus-check-api-reference` | Verify generated pdoc API reference output contains subpackage pages |
| `hephaestus-check-api-table-docs` | Enforce per-symbol `__all__` documentation in COMPATIBILITY.md |
| `hephaestus-check-cli-tier-docs` | Enforce console-script stability-tier documentation in COMPATIBILITY.md |
| `hephaestus-check-complexity` | Check cyclomatic complexity against a threshold |
| `hephaestus-check-coverage` | Check test coverage against configurable thresholds |
| `hephaestus-check-doc-config` | Enforce consistency between documentation metric values and authoritative config sources |
| `hephaestus-check-docstrings` | Check Python docstrings for genuine sentence fragments |
| `hephaestus-check-python-version` | Check Python version consistency across project configuration files |
| `hephaestus-check-readmes` | Markdown validation utilities for HomericIntelligence projects |
| `hephaestus-check-stale-scripts` | Detect scripts in `scripts/` with no references in CI configs or other scripts |
| `hephaestus-check-test-structure` | Validate unit test directory structure |
| `hephaestus-check-tier-labels` | Enforce tier label consistency across all project Markdown files |
| `hephaestus-check-type-aliases` | Detect type alias shadowing patterns in Python code |
| `hephaestus-check-unlinked-todo` | Enforce that every TODO/FIXME/HACK marker references a tracking issue |
| `hephaestus-filter-audit` | Filter pip-audit JSON output to fail only on HIGH/CRITICAL severity vulnerabilities |
| `hephaestus-mypy-each-file` | Run mypy on each file individually to avoid duplicate-module-name errors |
| `hephaestus-validate-agents` | YAML frontmatter extraction and validation for agent markdown files |
| `hephaestus-validate-links` | Markdown validation utilities for HomericIntelligence projects |
| `hephaestus-validate-schemas` | Validate YAML configuration files against JSON schemas |

### Markdown

| Command | Description |
|---|---|
| `hephaestus-check-links` | Fix or validate invalid absolute path links in markdown files |
| `hephaestus-fix-markdown` | Markdown linting fixer utilities for Hephaestus |
| `hephaestus-validate-anchors` | Validate anchor fragments in markdown links against actual headings |

### CI / Pre-commit

| Command | Description |
|---|---|
| `hephaestus-bench-precommit` | Pre-commit CI utilities for GitHub Actions integration (benchmark) |
| `hephaestus-check-workflow-inventory` | GitHub Actions workflow validation utilities (inventory check) |
| `hephaestus-validate-workflow-checkout` | GitHub Actions workflow validation utilities (checkout validation) |

### Development Utilities

| Command | Description |
|---|---|
| `hephaestus-scaffold-subpackage` | Scaffold a new hephaestus subpackage skeleton with matching test directory |

### Configuration & Dependencies

| Command | Description |
|---|---|

### Version Management

| Command | Description |
|---|---|
| `hephaestus-bump-version` | Version consistency checks and atomic version bumping |
| `hephaestus-check-package-versions` | Check package version consistency across config files |
| `hephaestus-check-version-consistency` | Version consistency checks across config files |

### Examples

```bash
# Collect system info (JSON output)
hephaestus-system-info --json

# Collect system info without tool version checks
hephaestus-system-info --no-tools

# Download a dataset
hephaestus-download-dataset --help

# Merge open PRs
hephaestus-merge-prs --help

# Run all validation checks
hephaestus-check-coverage --help
hephaestus-check-complexity --help
```

## Development Guidelines

1. Follow the principles in [AGENTS.md](AGENTS.md)
2. Write comprehensive unit tests for all new functionality
3. Document all public functions with Google-style docstrings
4. Use type hints for all function parameters and return values
5. Keep functions small and focused (single responsibility principle)

## Contributing

The `main` branch is protected; all changes go through a pull request. The
ruleset requires signed commits, while CI's `pr-policy` checks issue references,
Conventional Commit subjects, and DCO trailers. The loop runs
`$athena:pr-review` and then writes `state:implementation-go`; it arms only in
`merge_wait`. Normal review may collect CI/CD evidence and incorporate it into
its binary verdict, but the loop does not change CI/CD and no CI workflow
independently authorizes it.

1. Create a feature branch named `<issue-number>-description`
   (`git checkout -b 123-amazing-feature`).
2. Commit your changes **signed and DCO-signed** (`git commit -s -S -m "feat(scope): add amazing feature"`),
   using [conventional commit](https://www.conventionalcommits.org/) messages.
3. Push the branch (`git push -u origin 123-amazing-feature`).
4. Open a pull request whose body contains the literal line `Closes #123`
   (capital `C`, no colon, on its own line — `Fixes`/`Resolves` are **not** accepted).
5. Do not enable auto-merge manually. The automation loop's review, label, and
   `merge_wait` stages are its sole automatic authority.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full process.

## uv Environment

`uv sync` creates this checkout's `.venv` and installs the project in editable
mode. The default dependency groups include the development and automation
tools; run repository commands with `uv run <command>` so they use that locked
environment. Use `uv sync --all-groups --all-extras --locked` when a workflow
or local check must exercise the complete dependency surface represented by
`uv.lock`.

## Adding New Dependencies

Use uv to add a runtime dependency, then refresh the environment:

```bash
uv add requests
uv sync
```

## License

BSD 3-Clause License — see [LICENSE](LICENSE) for the full text, and
[NOTICE](NOTICE) for third-party dependency licenses and compatibility notes.
