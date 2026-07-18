# Hephaestus command runner — wraps uv commands for consistent developer experience.
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
    uv sync
    uv run pre-commit install

# Run all tests (unit + integration)
test:
    uv run pytest {{ test_dir }}

# Run unit tests only
test-unit:
    uv run pytest {{ unit_test_dir }}

# Run integration tests only
test-integration:
    uv run pytest {{ integration_test_dir }}

# Run the opt-in external contract lane (real gh; agent lane needs HEPHAESTUS_CONTRACT_AGENT=1)
test-contract:
    HEPHAESTUS_CONTRACT_TESTS=1 uv run pytest {{ integration_test_dir }}/contract --override-ini="addopts=" -v --strict-markers

# Run BATS shell tests (recursive under tests/shell)
test-shell:
    bats --recursive tests/shell

# Re-run unit tests on file change (uses pytest-watcher). Cancel with Ctrl-C.
watch:
    uv run ptw {{ unit_test_dir }} -- --no-cov -q

# Run linter
lint:
    uv run ruff check {{ src_dirs }}

# Run formatter
format:
    uv run ruff format {{ src_dirs }}

# Check formatting without applying changes
format-check:
    uv run ruff format --check {{ src_dirs }}

# Run type checking on the package only (use `uv run mypy hephaestus/ scripts/ tests/` for everything)
typecheck:
    uv run mypy {{ pkg_dir }}/

# Run all pre-commit hooks on all files
precommit:
    uv run pre-commit run --all-files

# Run lint + format-check + typecheck
check: lint format-check typecheck

# Audit dependencies for known vulnerabilities via the configured policy
# (hephaestus-filter-audit + .pip-audit-ignore.txt)
audit:
    uv run pip-audit --format json | uv run hephaestus-filter-audit

# Generate API reference documentation with pdoc (output: docs/api/)
docs:
    uv run pdoc ./hephaestus ./hephaestus/agents ./hephaestus/benchmarks ./hephaestus/ci ./hephaestus/cli ./hephaestus/config ./hephaestus/datasets ./hephaestus/discovery ./hephaestus/forensics ./hephaestus/github ./hephaestus/io ./hephaestus/logging ./hephaestus/markdown ./hephaestus/nats ./hephaestus/observability ./hephaestus/prompts ./hephaestus/resilience ./hephaestus/scripts_lib ./hephaestus/system ./hephaestus/utils ./hephaestus/validation ./hephaestus/version --output-dir docs/api

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
# (safe — only removes regenerable artifacts; does NOT touch .venv/ envs).
# NOTE: also wipes docs/api/ — pdoc-generated API docs (see `just docs`);
# regenerate them afterward. Does NOT remove build/ wholesale: this repo keeps
# live git worktrees under build/.worktrees/, so only build-backend outputs are
# pruned to avoid destroying sibling worktrees and their uncommitted work.
clean-all: clean
    rm -rf .ruff_cache .mypy_cache .pytest_cache htmlcov .coverage
    rm -rf dist docs/api
    rm -rf build/lib build/lib.* build/bdist.* build/temp.* build/scripts*
    find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
    find . -type d -name '*.egg-info' -not -path './.venv/*' -exec rm -rf {} +
    find . -type f \( -name '*.pyc' -o -name '*.pyo' \) -not -path './.venv/*' -delete
