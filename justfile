# ProjectHephaestus command runner — wraps pixi commands for consistent developer experience.
# All path variables are configurable at the top of the file.

# Source directories for linting, formatting, and type checking
src_dirs := "hephaestus scripts tests"

# Primary package source directory
pkg_dir := "hephaestus"

# Test directories
test_dir := "tests"
unit_test_dir := "tests/unit"
integration_test_dir := "tests/integration"

# List available recipes
default:
    @just --list

# Install dependencies and set up pre-commit hooks (one-command bootstrap)
bootstrap:
    pixi install
    pixi run pre-commit install

# Run all tests (unit + integration)
test:
    pixi run pytest {{ test_dir }}

# Run unit tests only
test-unit:
    pixi run pytest {{ unit_test_dir }}

# Run integration tests only
test-integration:
    pixi run pytest {{ integration_test_dir }}

# Run linter
lint:
    pixi run ruff check {{ src_dirs }}

# Run formatter
format:
    pixi run ruff format {{ src_dirs }}

# Check formatting without applying changes
format-check:
    pixi run ruff format --check {{ src_dirs }}

# Run type checking
typecheck:
    pixi run mypy {{ pkg_dir }}

# Run all pre-commit hooks on all files
precommit:
    pixi run pre-commit run --all-files

# Run lint + format-check + typecheck
check: lint format-check typecheck

# Run pip-audit to check for known dependency vulnerabilities
audit:
    pixi run --environment lint pip-audit

# Full CI-equivalent run: bootstrap, check, and test
all: bootstrap check test
