#!/bin/bash
# Run the Hephaestus CI suite locally inside a container.
#
# Mirrors what GitHub Actions runs, using the same CI container image.
# Supports both Podman (rootless, no SU — preferred) and Docker.
#
# Usage:
#   ./scripts/run_ci_local.sh              # Run all CI checks
#   ./scripts/run_ci_local.sh lint         # pre-commit + doc-link validation
#   ./scripts/run_ci_local.sh unit         # unit tests + structure/coverage checks
#   ./scripts/run_ci_local.sh integration  # integration tests
#   ./scripts/run_ci_local.sh cli          # installed-CLI entry-point tests
#   ./scripts/run_ci_local.sh build        # sdist + wheel build
#   ./scripts/run_ci_local.sh audit        # pip-audit dependency scan
#   ./scripts/run_ci_local.sh sast         # bandit static analysis
#   ./scripts/run_ci_local.sh workflow-scan # zizmor workflow security scan
#   ./scripts/run_ci_local.sh schema       # workflow YAML schema validation
#   ./scripts/run_ci_local.sh version      # version-single-source + uv.lock check
#   ./scripts/run_ci_local.sh license      # license compatibility scan
#
# Container engine: auto-detected (podman first, docker fallback).
# Override: CONTAINER_ENGINE=docker ./scripts/run_ci_local.sh
#
# Image: built locally from ci/Containerfile — run `just ci-build` first.

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SUBSET="${1:-all}"

LOCAL_IMAGE="hephaestus-ci:local"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[CI]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[CI]${NC} $*"; }
log_error() { echo -e "${RED}[CI]${NC} $*" >&2; }
log_step()  { echo -e "\n${BLUE}==>${NC} $*"; }

# ============================================================================
# Container engine detection
# ============================================================================

detect_engine() {
    if [ -n "${CONTAINER_ENGINE:-}" ]; then
        if ! command -v "${CONTAINER_ENGINE}" &> /dev/null; then
            log_error "CONTAINER_ENGINE=${CONTAINER_ENGINE} not found in PATH"
            exit 1
        fi
        log_info "Container engine: ${CONTAINER_ENGINE} (from env)"
        return
    fi

    if command -v podman &> /dev/null; then
        CONTAINER_ENGINE="podman"
        log_info "Container engine: podman (rootless)"
    elif command -v docker &> /dev/null; then
        CONTAINER_ENGINE="docker"
        log_info "Container engine: docker"
    else
        log_error "No container engine found. Install podman (recommended) or docker."
        log_error "  Podman: https://podman.io/getting-started/installation"
        exit 1
    fi
    export CONTAINER_ENGINE
}

# ============================================================================
# Image resolution
# ============================================================================

resolve_image() {
    if "${CONTAINER_ENGINE}" image exists "${LOCAL_IMAGE}" 2>/dev/null || \
       "${CONTAINER_ENGINE}" images -q "${LOCAL_IMAGE}" 2>/dev/null | grep -q .; then
        CI_IMAGE="${LOCAL_IMAGE}"
        log_info "Using local CI image: ${CI_IMAGE}"
    else
        log_error "Local image '${LOCAL_IMAGE}' not found."
        log_error "Build it first: just ci-build"
        log_error "  (podman build -f ci/Containerfile -t ${LOCAL_IMAGE} .)"
        exit 1
    fi
    export CI_IMAGE
}

# ============================================================================
# Run a command inside the CI container
# ============================================================================
# Volume mounts:
#   /workspace  — the full repo (rw, :Z for SELinux/Podman)
# --userns=keep-id:uid=1000,gid=1000 — run as the image's non-root 'ci' user
# while mapping it to the invoking host UID, so mounted-file ownership works on
# both dev hosts (uid 1000) and GitHub runners (uid 1001).
# --tmpfs /tmp — pytest tmp_path and the Pi smoke runtime use a real POSIX-ACL
# filesystem (tmpfs), which their verification requires; the default overlay
# filesystem of the container is not ACL-verifiable.

run_in_container() {
    local cmd=("$@")
    local engine_flags=()

    if [ "${CONTAINER_ENGINE}" = "podman" ]; then
        engine_flags+=(--userns=keep-id:uid=1000,gid=1000)
    fi

    "${CONTAINER_ENGINE}" run --rm \
        "${engine_flags[@]}" \
        --tmpfs /tmp:rw,size=4g,mode=1777 \
        --volume "${PROJECT_ROOT}:/workspace:Z" \
        --workdir /workspace \
        "${CI_IMAGE}" \
        "${cmd[@]}"
}

# ============================================================================
# CI steps
# ============================================================================

run_lint() {
    log_step "Lint (pre-commit + doc-link validation)"
    run_in_container uv run pre-commit run --all-files --show-diff-on-failure
    run_in_container uv run hephaestus-validate-links docs --repo-root .
}

run_unit() {
    log_step "Unit tests + structure/coverage checks"
    run_in_container bash -c '\
        uv run pytest tests/unit --override-ini="addopts=" -v --strict-markers -m "not nightly" \
            --cov=hephaestus --cov-report=xml --cov-report=term-missing && \
        uv run hephaestus-check-test-structure && \
        uv run hephaestus-check-coverage --coverage-file coverage.xml --config coverage.toml'
}

run_integration() {
    log_step "Integration tests"
    run_in_container uv run pytest tests/integration --override-ini="addopts=" -v --strict-markers -m "not nightly"
}

run_cli() {
    log_step "Installed-CLI entry-point tests"
    run_in_container bash -c '\
        uv build --wheel && \
        uv venv build/cli-venv && \
        WHEEL=(dist/*.whl) && \
        uv pip install --python build/cli-venv/bin/python "${WHEEL[0]}[automation]" pytest pyyaml && \
        export PATH="$PWD/build/cli-venv/bin:$PATH" && \
        HEPHAESTUS_REQUIRE_CLI=1 build/cli-venv/bin/pytest tests/integration/test_cli_entry_points.py \
            --override-ini="addopts=" -v --strict-markers'
}

run_build() {
    log_step "Package build (sdist + wheel)"
    run_in_container uv run python -m build --no-isolation
}

run_audit() {
    log_step "Dependency scan (pip-audit)"
    run_in_container uv run pip-audit
}

run_sast() {
    log_step "Static analysis (bandit)"
    run_in_container uv run bandit -c pyproject.toml -r hephaestus scripts --severity-level medium
}

run_workflow_scan() {
    log_step "Workflow security scan (zizmor)"
    run_in_container uv run zizmor --no-online-audits --min-severity medium .github/workflows/
}

run_schema() {
    log_step "Workflow YAML schema validation"
    run_in_container uvx check-jsonschema \
        --builtin-schema vendor.github-workflows \
        .github/workflows/*.yml
}

run_version() {
    log_step "Version single-source-of-truth + uv.lock check"
    run_in_container uv run python -m hephaestus.scripts_lib.check_version_single_source
    run_in_container uv lock --check
}

run_license() {
    log_step "License compatibility scan"
    run_in_container uv run python scripts/check_license_compatibility.py
}

# ============================================================================
# Main
# ============================================================================

FAILED=()

run_step() {
    local name="$1"
    local fn="$2"
    if ! "${fn}"; then
        FAILED+=("${name}")
        log_error "${name} FAILED"
    fi
}

detect_engine
resolve_image

log_info "CI subset: ${SUBSET}"
log_info "Project root: ${PROJECT_ROOT}"

case "${SUBSET}" in
    lint)
        run_step "lint" run_lint
        ;;
    unit)
        run_step "unit" run_unit
        ;;
    integration)
        run_step "integration" run_integration
        ;;
    cli)
        run_step "cli" run_cli
        ;;
    build)
        run_step "build" run_build
        ;;
    audit)
        run_step "audit" run_audit
        ;;
    sast)
        run_step "sast" run_sast
        ;;
    workflow-scan)
        run_step "workflow-scan" run_workflow_scan
        ;;
    schema)
        run_step "schema" run_schema
        ;;
    version)
        run_step "version" run_version
        ;;
    license)
        run_step "license" run_license
        ;;
    all)
        run_step "lint" run_lint
        run_step "unit" run_unit
        run_step "integration" run_integration
        run_step "cli" run_cli
        run_step "build" run_build
        run_step "audit" run_audit
        run_step "sast" run_sast
        run_step "workflow-scan" run_workflow_scan
        run_step "schema" run_schema
        run_step "version" run_version
        run_step "license" run_license
        ;;
    *)
        log_error "Unknown subset: ${SUBSET}"
        log_error "Valid values: all, lint, unit, integration, cli, build, audit, sast, workflow-scan, schema, version, license"
        exit 1
        ;;
esac

echo ""
if [ "${#FAILED[@]}" -eq 0 ]; then
    log_info "All CI checks passed."
else
    log_error "Failed: ${FAILED[*]}"
    exit 1
fi
