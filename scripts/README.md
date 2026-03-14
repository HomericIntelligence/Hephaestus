# Scripts Directory

This directory contains CLI wrapper scripts for ProjectHephaestus utilities.

## Available Scripts

### Validation Scripts

- **validate_links.py** - Validate markdown links in documentation
- **validate_structure.py** - Validate repository directory structure
- **check_readmes.py** - Check README files for completeness
- **lint_configs.py** - Lint YAML configuration files

### Git/GitHub Scripts

- **generate_changelog.py** - Generate changelog from git commit history
- **merge_prs.py** - Merge pull requests with successful CI/CD

### Demo/Testing Scripts

- **demo_cli.py** - Demo CLI functionality
- **run_tests.py** - Run test suite
- **example_usage.py** - Usage examples

## Usage

All scripts are executable and can be run directly:

```bash
# Generate changelog
python scripts/generate_changelog.py

# Merge PRs (requires GITHUB_TOKEN)
python scripts/merge_prs.py --dry-run

# Validate structure (requires configuration)
python scripts/validate_structure.py
```

## Source Repositories

Scripts ported from:

- **ProjectOdyssey**: Validation, changelog generation, PR merging
- **ProjectHephaestus**: Original utilities and helpers

## Design Principles

Following CLAUDE.md guidelines:

- **KISS** (Keep It Simple, Stupid) - Scripts are thin wrappers
- **DRY** (Don't Repeat Yourself) - Logic in hephaestus modules
- **YAGNI** (You Aren't Gonna Need It) - Only port what's reusable
- **Modularity** - Clear separation between CLI and core logic
