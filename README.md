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
├── src/               # Source code
│   └── hephaestus/    # Main package
│       ├── __init__.py
│       ├── utils/     # Utility functions
│       ├── config/    # Configuration utilities
│       └── io/        # I/O utilities
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
# Format code with Black
pixi run format

# Lint code with Ruff
pixi run lint

# Build documentation
pixi run docs-build
```

## Usage

### As a Package

After installing with Pixi:

```python
from hephaestus import slugify, human_readable_size
from hephaestus.utils.general import retry_with_backoff

# Convert text to URL-friendly slug
project_slug = slugify("My Project Name")
print(project_slug)  # Output: my-project-name

# Convert bytes to human readable size
size_str = human_readable_size(1048576)
print(size_str)  # Output: 1.0 MB
```

### Installing in Another Project

To use ProjectHephaestus in another project with Pixi:

1. Add it as a dependency in your `pixi.toml`:
   ```toml
   [pypi-dependencies]
   project-hephaestus = { path = "../ProjectHephaestus", editable = true }
   ```

2. Run `pixi install` to install the dependency

## Key Features

### General Utilities (`hephaestus.utils.general`)

- `slugify(text)`: Convert text to URL-friendly slug
- `retry_with_backoff(func)`: Decorator for exponential backoff retries
- `human_readable_size(bytes)`: Convert bytes to human readable format
- `flatten_dict(dict)`: Flatten nested dictionaries
- `run_command(cmd)`: Execute shell commands with error handling
- `get_nested_value(data, key_path)`: Get nested dict values with dot notation

### Configuration (`hephaestus.config`)

*Coming soon: Standardized configuration management*

### I/O Utilities (`hephaestus.io`)

*Coming soon: File I/O utilities with standardized interfaces*

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
- **docs**: Documentation building environment

Switch between environments with:
```bash
pixi shell -e dev
pixi shell -e docs
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

MIT License - see [LICENSE](LICENSE) file for details.
