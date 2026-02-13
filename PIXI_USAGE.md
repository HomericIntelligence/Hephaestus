# Pixi Usage Guide for ProjectHephaestus

## Overview

ProjectHephaestus uses [Pixi](https://pixi.sh) for Python dependency and environment management. All Python commands should be run through `pixi run`.

## Quick Start

### Install Pixi

```bash
# On macOS/Linux
curl -fsSL https://pixi.sh/install.sh | bash

# Or via conda/mamba
conda install -c conda-forge pixi
```

### Basic Commands

```bash
# Install all dependencies (development environment)
pixi install

# Run pytest
pixi run pytest tests/ -v

# Run specific test file
pixi run pytest tests/test_general_utils.py -v

# Run with coverage
pixi run pytest --cov=hephaestus --cov-report=term-missing

# Run linters
pixi run flake8 hephaestus
pixi run black --check hephaestus tests
pixi run mypy hephaestus --ignore-missing-imports

# Format code
pixi run black hephaestus tests

# Install pre-commit hooks
pixi run pre-commit install

# Run pre-commit manually
pixi run pre-commit run --all-files
```

## Pre-commit Hooks

Pre-commit hooks run automatically on `git commit` to ensure code quality.

### Setup

```bash
# One-time setup
pixi run pre-commit install
```

### What Runs

On every commit:
- ✓ Remove trailing whitespace
- ✓ Ensure files end with newline
- ✓ Check YAML/TOML syntax
- ✓ Prevent large files
- ✓ Check for merge conflicts
- ✓ Remove debug statements
- ✓ Format with black
- ✓ Lint with flake8
- ✓ Type check with mypy

### Manual Run

```bash
# Run on all files
pixi run pre-commit run --all-files

# Run specific hook
pixi run pre-commit run black --all-files

# Skip hooks (use sparingly)
git commit --no-verify -m "message"
```

## CI/CD Integration

The GitHub Actions workflow automatically uses Pixi:

```yaml
- name: Install Pixi
  uses: prefix-dev/setup-pixi@v0.4.1
  with:
    pixi-version: latest

- name: Install dependencies
  run: pixi install

- name: Run tests
  run: pixi run pytest tests/ -v
```

## Common Tasks

### Running Tests

```bash
# All tests
pixi run pytest

# Verbose output
pixi run pytest -v

# Stop on first failure
pixi run pytest -x

# Run only unit tests
pixi run pytest -m unit

# Run with coverage
pixi run pytest --cov=hephaestus

# Generate HTML coverage report
pixi run pytest --cov=hephaestus --cov-report=html
# Then open htmlcov/index.html
```

### Code Quality

```bash
# Check formatting (don't modify)
pixi run black --check hephaestus tests

# Format code (modify in place)
pixi run black hephaestus tests

# Lint
pixi run flake8 hephaestus

# Type check
pixi run mypy hephaestus
```

### Development Workflow

```bash
# 1. Install dependencies
pixi install

# 2. Set up pre-commit
pixi run pre-commit install

# 3. Make changes to code

# 4. Run tests
pixi run pytest tests/ -v

# 5. Check formatting
pixi run black hephaestus tests

# 6. Commit (pre-commit runs automatically)
git commit -m "feat: Add new feature"

# 7. Push
git push
```

## Troubleshooting

### Pixi Not Found

```bash
# Make sure pixi is in PATH
which pixi

# If not, add to PATH or reinstall
curl -fsSL https://pixi.sh/install.sh | bash
```

### Dependencies Not Installing

```bash
# Clear cache and reinstall
pixi clean
pixi install
```

### Pre-commit Failing

```bash
# Update hooks
pixi run pre-commit autoupdate

# Clean and reinstall
pixi run pre-commit clean
pixi run pre-commit install
```

### Tests Failing

```bash
# Verify installation
pixi run python -c "import hephaestus; print(hephaestus.__version__)"

# Check dependencies
pixi list

# Reinstall
pixi clean
pixi install
```

## Comparison: pip vs pixi

### Old Way (pip)
```bash
pip install -e .[dev]
pytest tests/ -v
black --check hephaestus
```

### New Way (pixi)
```bash
pixi install
pixi run pytest tests/ -v
pixi run black --check hephaestus
```

## Benefits of Pixi

- ✅ **Reproducible environments** - Same versions everywhere
- ✅ **Fast dependency resolution** - Conda-based solver
- ✅ **Isolated environments** - No global pollution
- ✅ **Cross-platform** - Works on macOS, Linux, Windows
- ✅ **Simple** - Single command to set up
- ✅ **Integrated** - Works with pip, conda, and PyPI

## Configuration Files

- `pixi.toml` - Main pixi configuration
- `.pre-commit-config.yaml` - Pre-commit hooks
- `pytest.ini` - Pytest configuration
- `.github/workflows/ci.yml` - CI/CD with pixi

## Scripts Shorthand

If you get tired of typing `pixi run`:

```bash
# Add to ~/.bashrc or ~/.zshrc
alias pr='pixi run'

# Then use:
pr pytest tests/ -v
pr black hephaestus
pr pre-commit run --all-files
```

## Documentation

- [Pixi Documentation](https://pixi.sh/latest/)
- [Pre-commit Documentation](https://pre-commit.com/)
- [pytest Documentation](https://docs.pytest.org/)

## Support

If you encounter issues:
1. Check `pixi list` to see installed packages
2. Try `pixi clean && pixi install`
3. Verify pixi version: `pixi --version`
4. Check logs in `.pixi/` directory
