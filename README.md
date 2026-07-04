# ProjectHephaestus

[![Test](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/test.yml/badge.svg)](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/test.yml)
[![Pre-commit](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/pre-commit.yml/badge.svg)](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/pre-commit.yml)
[![Security](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/security.yml/badge.svg)](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/security.yml)
[![Release](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/release.yml/badge.svg)](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/release.yml)
[![Auto Tag](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/auto-tag.yml/badge.svg)](https://github.com/HomericIntelligence/ProjectHephaestus/actions/workflows/auto-tag.yml)
[![PyPI](https://img.shields.io/pypi/v/HomericIntelligence-Hephaestus.svg)](https://pypi.org/project/HomericIntelligence-Hephaestus/)
[![Python](https://img.shields.io/pypi/pyversions/HomericIntelligence-Hephaestus.svg)](https://pypi.org/project/HomericIntelligence-Hephaestus/)
[![License: BSD-3-Clause](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)

Shared utilities and tooling for the HomericIntelligence ecosystem, powered by [Pixi](https://pixi.sh) for environment management.

## Overview

ProjectHephaestus provides standardized utility functions and tools that can be shared across all HomericIntelligence repositories. Following the principles in [CLAUDE.md](CLAUDE.md), this project emphasizes:

- **Modularity**: Well-defined, reusable components
- **Simplicity**: KISS (Keep It Simple, Stupid) principle
- **Consistency**: Standardized interfaces and patterns
- **Reliability**: Comprehensive testing and error handling

**Project Status:** See [docs/ROADMAP.md](docs/ROADMAP.md) for the public roadmap and current focus areas.

## Installation

### From PyPI

ProjectHephaestus is published to PyPI under the ecosystem-branded distribution name **`HomericIntelligence-Hephaestus`**. The import name, however, is the short lowercase `hephaestus`:

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
> **Note on naming.** `pip install hephaestus` and `pip install project-hephaestus` will **not** find this package — both names are unowned on PyPI. The `HomericIntelligence-<Project>` prefix is the deliberate naming convention shared across the HomericIntelligence ecosystem (ProjectKeystone, ProjectOdyssey, etc.) to avoid PyPI namespace collisions. Wheel filenames are PEP 625 normalized to lowercase, so you will see `homericintelligence_hephaestus-<version>-py3-none-any.whl` on disk and in release assets.

### Optional dependencies

`pyproject.toml` defines several extras groups. `[all]` is a **runtime** aggregator
and intentionally excludes `[dev]` (which carries test/lint tooling such as
pytest, ruff, and mypy):

- `pip install HomericIntelligence-Hephaestus[all]` — installs all runtime
  extras: `automation`, `github`, `nats`, `toml`, `xml`, `schema`. Note that
  `automation` is the product layer (`hephaestus.automation`) and pulls in
  `pydantic`; see [ADR 0001](docs/adr/0001-automation-library-boundary.md).
- `pip install "HomericIntelligence-Hephaestus[mesh]"` — installs the HMAS
  mesh-worker dependencies (`automation` + `nats`) needed by
  `hephaestus-mesh-worker`; deliberately not part of `[all]`.
- `pip install HomericIntelligence-Hephaestus[dev]` — installs development and
  testing dependencies. Use this for contributors and CI.
- `pip install "HomericIntelligence-Hephaestus[all,dev]"` — both, for a full
  development environment via pip (Pixi users get this automatically via
  `pixi install`).
- Individual extras (e.g. `[github]`, `[schema]`) are available for users who
  only need one integration.

### Development setup

For local development, [install Pixi](https://pixi.sh/install/) and
[`just`](https://just.systems/), then bootstrap the project (installs deps, the
editable package, and pre-commit hooks in one step):

```bash
just bootstrap
```

See [CONTRIBUTING.md → Development Setup](CONTRIBUTING.md#development-setup) for
the full workflow, including the manual fallback if you do not have `just`.

## Library vs product layer

ProjectHephaestus ships two layers from one distribution:

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
ProjectHephaestus/
├── pixi.toml          # Pixi configuration
├── pyproject.toml     # Python package configuration
├── hephaestus/        # Main package
│   ├── __init__.py
│   ├── agents/        # Agent frontmatter + loader + runtime
│   ├── automation/    # Issue planning / implementation / PR review pipeline
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
│   ├── resilience/    # Circuit breaker + retry + subprocess resilience primitives
│   ├── scripts_lib/   # Standalone consistency-check scripts (CLI table, version)
│   ├── system/        # System information collection
│   ├── utils/         # General utility functions (slugify, retry, subprocess)
│   ├── validation/    # README, schema, and structural validation
│   └── version/       # Version management (hatch-vcs + consistency checks)
├── tests/             # Unit tests
├── docs/              # Documentation
├── scripts/           # Utility scripts
└── README.md          # This file
```

## Getting Started with Pixi

This project uses [Pixi](https://pixi.sh) for environment management, which automatically handles dependencies and creates isolated environments.

> **Platform note:** The pixi developer environment is **Linux-64 only** (see
> `platforms` in [`pixi.toml`](pixi.toml)). On macOS or Windows, install the
> published wheel into a plain virtualenv instead — see
> [From PyPI](#from-pypi) above. The
> full comparison table (install paths, supported platforms, Python versions)
> lives in [CONTRIBUTING.md#platform-support](CONTRIBUTING.md#platform-support).

### Prerequisites

Install Pixi by following the [official installation guide](https://pixi.sh/install/).

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
pixi run pytest

# Run only unit tests (coverage-gated in CI)
just test-unit
pytest -m unit

# Run only integration tests
just test-integration
pytest -m integration

# Run all tests except integration
pytest -m "not integration"
```

All integration tests carry `pytest.mark.integration` (module-level `pytestmark`),
so marker-based selection is reliable.

### Development Commands

```bash
# Format code with ruff
pixi run format

# Lint code with ruff
pixi run lint
```

## Usage

### As a Package

After installing with Pixi:

```python
from hephaestus import slugify, human_readable_size, retry_with_backoff

# Convert text to URL-friendly slug
project_slug = slugify("My Project Name")
print(project_slug)  # Output: my-project-name

# Convert bytes to human readable size
size_str = human_readable_size(1048576)
print(size_str)  # Output: 1.0 MB
```

### As a Claude Code Plugin

ProjectHephaestus also ships as a Claude Code plugin, providing slash commands for repository auditing, agent orchestration, and knowledge management.

```bash
claude plugin install HomericIntelligence/ProjectHephaestus
```

Then enable it in your project's `.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "hephaestus@ProjectHephaestus": true
  }
}
```

See [docs/plugin-installation.md](docs/plugin-installation.md) for the full installation guide and skill reference.

### As a Codex Plugin

ProjectHephaestus also ships Codex plugin metadata for the same `hephaestus` skill set.

```bash
codex plugin marketplace add HomericIntelligence/ProjectHephaestus --ref main
codex plugin add hephaestus@project-hephaestus
```

The Codex manifest lives in [`.codex-plugin/plugin.json`](.codex-plugin/plugin.json), and the marketplace entry lives in [`.agents/plugins/marketplace.json`](.agents/plugins/marketplace.json).

### Installing in Another Project

ProjectHephaestus is published to PyPI as `homericintelligence-hephaestus`.
The wheel is pure-Python and installs on Linux, macOS, and Windows
(see `requires-python` in [`pyproject.toml`](pyproject.toml)). This is
the supported install path for non-Linux platforms.

**Using pip:**

```bash
pip install homericintelligence-hephaestus
```

**Using Pixi:**

Add to `pyproject.toml`:

```toml
[project]
dependencies = [
    "homericintelligence-hephaestus>=0.9,<1",
]
```

Or add a PyPI entry to `pixi.toml`:

```toml
[pypi-dependencies]
homericintelligence-hephaestus = ">=0.9,<1"
```

Then run `pixi install` to resolve the dependency.

After 1.0 ships, bump these constraints to `>=1.0,<2`.

**For local development (path dependency):**

```toml
[pypi-dependencies]
homericintelligence-hephaestus = { path = "../ProjectHephaestus", editable = true }
```

## Key Features

### General Utilities (`hephaestus.utils`)

- `slugify(text)`: Convert text to URL-friendly slug
- `retry_with_backoff(func)`: Decorator for exponential backoff retries
- `human_readable_size(bytes)`: Convert bytes to human readable format
- `flatten_dict(dict)`: Flatten nested dictionaries
- `run_subprocess(cmd)`: Execute shell commands with error handling
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

<!-- CLI table generated from pyproject.toml [project.scripts]. Keep in sync via
     `python3 -m hephaestus.scripts_lib.check_cli_table_sync` (also enforced in pre-commit). -->

53 console scripts are installed when you install the package.  Run any command
with `--help` to see full usage.

### Automation

| Command | Description |
|---|---|
| `hephaestus-automation-loop` | Multi-repo 3-stage automation pipeline using Claude Code or Codex (plan → implement → drive-green; plan-review and PR-review/address-review run in-loop within plan/implement) |
| `hephaestus-mesh-worker` | HMAS mesh worker: serve one role-addressed (domain, role) NATS work queue (Odysseus ADR-013) |
| `hephaestus-plan-issues` | Bulk issue planning using Claude Code or Codex |
| `hephaestus-implement-issues` | Bulk issue implementation using Claude Code or Codex in parallel worktrees |
| `hephaestus-review-prs` | Read-only PR review automation using Claude Code or Codex in parallel worktrees |
| `hephaestus-agent-stage` | Run one Claude or Codex automation stage with prompt and skill context |
| `hephaestus-ensure-state-labels` | Idempotently provision `state:needs-plan` / `state:plan-no-go` / `state:plan-go` labels on one or more repos |
| `hephaestus-audit-prs` | Audit ALL open PRs in one coordinator agent invocation |

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
| `hephaestus-download-dataset` | Dataset downloading utilities for ProjectHephaestus |
| `hephaestus-system-info` | System information collection utilities for ProjectHephaestus |

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
| `hephaestus-check-skill-catalog` | Ensure `docs/plugin-installation.md` lists every skill shipped under `skills/` and validates skill frontmatter |
| `hephaestus-check-repo-analyze-skills` | Regenerate the six `skills/repo-analyze*/SKILL.md` from `skills/_repo_analyze_common/` partials; `--check` (default) enforces drift, `--write` updates in place |
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
| `hephaestus-fix-markdown` | Markdown linting fixer utilities for ProjectHephaestus |
| `hephaestus-validate-anchors` | Validate anchor fragments in markdown links against actual headings |

### CI / Pre-commit

| Command | Description |
|---|---|
| `hephaestus-bench-precommit` | Pre-commit CI utilities for GitHub Actions integration (benchmark) |
| `hephaestus-check-precommit-versions` | Pre-commit CI utilities for GitHub Actions integration (version check) |
| `hephaestus-check-workflow-inventory` | GitHub Actions workflow validation utilities (inventory check) |
| `hephaestus-validate-workflow-checkout` | GitHub Actions workflow validation utilities (checkout validation) |

### Development Utilities

| Command | Description |
|---|---|
| `hephaestus-scaffold-subpackage` | Scaffold a new hephaestus subpackage skeleton with matching test directory |

### Configuration & Dependencies

| Command | Description |
|---|---|
| `hephaestus-check-dep-sync` | Validate and synchronize dependency declarations across project config files |
| `hephaestus-sync-requirements` | Synchronize dependency declarations across project config files |

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

1. Follow the principles in [CLAUDE.md](CLAUDE.md)
2. Write comprehensive unit tests for all new functionality
3. Document all public functions with Google-style docstrings
4. Use type hints for all function parameters and return values
5. Keep functions small and focused (single responsibility principle)

## Contributing

The `main` branch is protected; all changes go through a pull request. CI enforces
three rules — a PR that violates any of them is blocked:

1. Create a feature branch named `<issue-number>-description`
   (`git checkout -b 123-amazing-feature`).
2. Commit your changes **signed** (`git commit -S -m "feat(scope): add amazing feature"`),
   using [conventional commit](https://www.conventionalcommits.org/) messages.
3. Push the branch (`git push -u origin 123-amazing-feature`).
4. Open a pull request whose body contains the literal line `Closes #123`
   (capital `C`, no colon, on its own line — `Fixes`/`Resolves` are **not** accepted).
5. Keep auto-merge disabled until implementation review applies
   `state:implementation-go`; then enable it with `gh pr merge --auto --squash`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full process.

## Pixi Environments

This project defines multiple environments in `pixi.toml`:

- **default**: Basic runtime environment
- **dev**: Development environment with linting and formatting tools
- **lint**: Linting-only environment

Switch environments with:

```bash
pixi shell -e dev
pixi shell -e lint
```

## Adding New Dependencies

Add new dependencies to `pixi.toml`:

For conda packages:

```toml
[dependencies]
numpy = "*"
```

For PyPI packages:

```toml
[pypi-dependencies]
requests = "*"
```

Then run:

```bash
pixi install
```

## License

BSD 3-Clause License — see [LICENSE](LICENSE) for the full text, and
[NOTICE](NOTICE) for third-party dependency licenses and compatibility notes.
