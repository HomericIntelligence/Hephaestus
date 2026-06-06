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
    pixi run dev-install
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

# Run BATS shell tests (recursive under tests/shell)
test-shell:
    pixi run test-shell

# Re-run unit tests on file change (uses pytest-watcher). Cancel with Ctrl-C.
watch:
    pixi run --environment default ptw {{ unit_test_dir }} -- --no-cov -q

# Run linter
lint:
    pixi run ruff check {{ src_dirs }}

# Run formatter
format:
    pixi run ruff format {{ src_dirs }}

# Check formatting without applying changes
format-check:
    pixi run ruff format --check {{ src_dirs }}

# Run type checking on the package only (use `pixi run mypy` for everything)
typecheck:
    pixi run mypy-pkg

# Run all pre-commit hooks on all files
precommit:
    pixi run pre-commit run --all-files

# Run lint + format-check + typecheck
check: lint format-check typecheck

# Run pip-audit to check for known dependency vulnerabilities
audit:
    pixi run --environment lint pip-audit

# Generate API reference documentation with pdoc (output: docs/api/)
docs:
    pixi run docs

# Full CI-equivalent run: bootstrap, check, and test
all: bootstrap check test

# Check HomericIntelligence ecosystem dependencies (check-only mode)
install-check:
    bash scripts/shell/install.sh

# Install missing HomericIntelligence ecosystem dependencies
install ROLE="all":
    bash scripts/shell/install.sh --install --role {{ ROLE }}

# Remove dev-run log files (run*.log are gitignored clutter)
clean:
    rm -f run*.log

# Deep clean: remove dev logs + all gitignored caches and build artifacts
# (safe — only removes regenerable artifacts; does NOT touch .pixi/ envs)
clean-all: clean
    rm -rf .ruff_cache .mypy_cache .pytest_cache htmlcov .coverage
    rm -rf build dist docs/api
    find . -type d -name __pycache__ -not -path './.pixi/*' -exec rm -rf {} +
    find . -type d -name '*.egg-info' -not -path './.pixi/*' -exec rm -rf {} +
    find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -not -path './.pixi/*' -delete
