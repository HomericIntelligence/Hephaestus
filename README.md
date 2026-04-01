# ProjectHephaestus

Shared utilities and tooling for the HomericIntelligence ecosystem, powered by [Pixi](https://pixi.sh) for environment management.

## Overview

ProjectHephaestus provides standardized utility functions and tools that can be shared across all HomericIntelligence repositories. Following the principles in [CLAUDE.md](CLAUDE.md), this project emphasizes:

- **Modularity**: Well-defined, reusable components
- **Simplicity**: KISS (Keep It Simple, Stupid) principle
- **Consistency**: Standardized interfaces and patterns
- **Reliability**: Comprehensive testing and error handling

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
│   ├── git/           # Git utilities (changelog generation)
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

To use ProjectHephaestus in another project with Pixi:

1. Add it as a dependency in your `pixi.toml`:

   ```toml
   [pypi-dependencies]
   hephaestus = { path = "../ProjectHephaestus", editable = true }
   ```

2. Run `pixi install` to install the dependency

Or install directly with pip:

```bash
pip install -e /path/to/ProjectHephaestus
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

Four command-line tools are installed as console scripts when you install the package:

| Command | Description |
|---|---|
| `hephaestus-changelog` | Generate a changelog from Git history |
| `hephaestus-merge-prs` | Automate merging of GitHub pull requests |
| `hephaestus-system-info` | Collect and display system/environment information |
| `hephaestus-download-dataset` | Download datasets with retry and progress reporting |

### Examples

```bash
# Generate changelog
hephaestus-changelog --help

# Collect system info (JSON output)
hephaestus-system-info --json

# Collect system info without tool version checks
hephaestus-system-info --no-tools

# Download a dataset
hephaestus-download-dataset --help

# Merge open PRs
hephaestus-merge-prs --help
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
