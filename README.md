# ProjectHephaestus

Shared utilities and tooling for the HomericIntelligence ecosystem, powered by [Pixi](https://pixi.sh) for environment management.

## Overview

ProjectHephaestus provides standardized utility functions and tools that can be shared across all HomericIntelligence repositories. Following the principles in [CLAUDE.md](CLAUDE.md), this project emphasizes:

- **Modularity**: Well-defined, reusable components
- **Simplicity**: KISS (Keep It Simple, Stupid) principle
- **Consistency**: Standardized interfaces and patterns
- **Reliability**: Comprehensive testing and error handling

**Project Status:** See [docs/ROADMAP.md](docs/ROADMAP.md) for the public roadmap and current focus areas.

## Directory Structure

```
ProjectHephaestus/
├── pixi.toml          # Pixi configuration
├── pyproject.toml     # Python package configuration
├── hephaestus/        # Main package
│   ├── __init__.py
│   ├── utils/         # General utility functions (slugify, retry, subprocess)
│   ├── config/        # Configuration utilities (YAML, JSON, env vars)
│   ├── io/            # I/O utilities (read, write, safe_write, load/save data)
│   ├── cli/           # CLI helpers (argument parsing, output formatting)
│   ├── logging/       # Logging utilities (ContextLogger, setup_logging)
│   ├── system/        # System information collection
│   ├── github/        # GitHub automation (PR merging)
│   ├── datasets/      # Dataset downloading utilities
│   ├── markdown/      # Markdown linting and link fixing
│   ├── benchmarks/    # Benchmark comparison utilities
│   ├── version/       # Version management
│   └── validation/    # README and config validation
├── tests/             # Unit tests
├── docs/              # Documentation
├── scripts/           # Utility scripts
└── README.md          # This file
```

## Getting Started with Pixi

This project uses [Pixi](https://pixi.sh) for environment management, which automatically handles dependencies and creates isolated environments.

### Prerequisites

Install Pixi by following the [official installation guide](https://pixi.sh/install/).

### Setup Development Environment

```bash
# Install dependencies and create environment
pixi install

# Activate the environment (optional, as pixi runs commands in the environment automatically)
pixi shell
```

### Running Tests

```bash
# Run tests using the test feature
pixi run test

# Or run tests directly with pytest
pixi run pytest
```

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

### Installing in Another Project

ProjectHephaestus is published to PyPI as `homericintelligence-hephaestus`.

**Using pip:**

```bash
pip install homericintelligence-hephaestus
```

**Using Pixi:**

Add to `pyproject.toml`:

```toml
[project]
dependencies = [
    "homericintelligence-hephaestus>=0.6.0,<1",
]
```

Or add a PyPI entry to `pixi.toml`:

```toml
[pypi-dependencies]
homericintelligence-hephaestus = ">=0.6.0,<1"
```

Then run `pixi install` to resolve the dependency.

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
     `python3 scripts/check_cli_table_sync.py` (also enforced in pre-commit). -->

38 console scripts are installed when you install the package.  Run any command
with `--help` to see full usage.

### Automation

| Command | Description |
|---|---|
| `hephaestus-plan-issues` | Bulk issue planning using Claude Code |
| `hephaestus-implement-issues` | Bulk issue implementation using Claude Code in parallel worktrees |
| `hephaestus-review-prs` | Read-only PR review automation using Claude Code in parallel worktrees |

### GitHub

| Command | Description |
|---|---|
| `hephaestus-fleet-sync` | Sync all PRs across the HomericIntelligence fleet |
| `hephaestus-github-stats` | GitHub contribution statistics via the `gh` CLI |
| `hephaestus-merge-prs` | Merge open PRs with successful CI/CD using GitHub API |
| `hephaestus-tidy` | Single-repo gh-tidy wrapper with Myrmidon swarm for conflict resolution |

### System & Data

| Command | Description |
|---|---|
| `hephaestus-agent-stats` | Agent statistics aggregation and reporting |
| `hephaestus-download-dataset` | Dataset downloading utilities for ProjectHephaestus |
| `hephaestus-system-info` | System information collection utilities for ProjectHephaestus |

### Validation

| Command | Description |
|---|---|
| `hephaestus-audit-doc-policy` | Audit documentation command examples for policy violations |
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

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

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

BSD 3-Clause License - see [LICENSE](LICENSE) file for details.
