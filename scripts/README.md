# Scripts Directory

This directory contains CLI wrapper scripts for ProjectHephaestus utilities.

## Available Scripts

### Validation Scripts

- **check_unit_test_structure.py** - Verify unit test directory mirrors source structure
- **check_python_version_consistency.py** - Check Python version consistency across config files
- **validate_readme_commands.py** - Validate commands in README code blocks

### Markdown Scripts

- **fix_invalid_links.py** - Fix invalid absolute path links in markdown files

### Git/GitHub Scripts

- **generate_changelog.py** - Generate changelog from git commit history
- **merge_prs.py** - Merge pull requests with successful CI/CD

### Version Scripts

- **update_version.py** - Update version numbers across project files

### Benchmark Scripts

- **compare_benchmarks.py** - Compare benchmark results across runs

### Demo/Testing Scripts

- **demo_cli.py** - Demo CLI functionality
- **run_tests.py** - Run test suite
- **example_usage.py** - Usage examples

## Usage

```bash
# Check unit test structure
python scripts/check_unit_test_structure.py

# Generate changelog
python scripts/generate_changelog.py

# Merge PRs (requires GITHUB_TOKEN)
python scripts/merge_prs.py --dry-run

# Fix invalid markdown links
python scripts/fix_invalid_links.py .

# Validate README commands
python scripts/validate_readme_commands.py

# Update project version
python scripts/update_version.py 0.4.0
```

## Design Principles

Following CLAUDE.md guidelines:

- **KISS** (Keep It Simple, Stupid) - Scripts are thin wrappers
- **DRY** (Don't Repeat Yourself) - Logic lives in hephaestus modules
- **YAGNI** (You Aren't Gonna Need It) - Only port what's reusable
- **Modularity** - Clear separation between CLI and core logic
