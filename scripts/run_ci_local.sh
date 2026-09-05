#!/bin/bash
# Run the locally executable Hephaestus CI checks.
#
# Project toolchain commands use the same CI container image as GitHub Actions
# when a usable container engine is available. On macOS, the complete pre-PR
# check can request a queue-owned native run for approved runner failures.
# Supports both Podman (rootless, no SU — preferred) and Docker.
#
# Usage:
#   ./scripts/run_ci_local.sh              # Run all local CI checks
#   ./scripts/run_ci_local.sh lint         # pre-commit + doc-link validation
#   ./scripts/run_ci_local.sh unit         # unit tests + structure/coverage checks
#   ./scripts/run_ci_local.sh integration  # integration tests
#   ./scripts/run_ci_local.sh cli          # installed-CLI entry-point tests
#   ./scripts/run_ci_local.sh build        # artifact + package lifecycle checks
#   ./scripts/run_ci_local.sh audit        # pip-audit dependency scan
#   ./scripts/run_ci_local.sh sast         # bandit static analysis
#   ./scripts/run_ci_local.sh workflow-scan # zizmor workflow security scan
#   ./scripts/run_ci_local.sh schema       # workflow YAML schema validation
#   ./scripts/run_ci_local.sh version      # version-single-source + uv.lock check
#   ./scripts/run_ci_local.sh license      # license compatibility scan
#   ./scripts/run_ci_local.sh symlinks     # repository symlink validation
#   ./scripts/run_ci_local.sh justfile     # justfile evaluation and recipe listing
#   ./scripts/run_ci_local.sh shellcheck   # shell static analysis
#   ./scripts/run_ci_local.sh shell-tests  # Bats shell test suite
#   ./scripts/run_ci_local.sh secrets      # Gitleaks repository scan
#
# Container engine: auto-detected (podman first, docker fallback).
# Override: CONTAINER_ENGINE=docker ./scripts/run_ci_local.sh
#
# Image: built locally from ci/Containerfile when it is not already present.
# Pass --rebuild to rebuild from the current checkout even when the local tag
# exists. The autonomous queue uses this mode.
# `just ci-build` remains available for an explicit warm-up.

set -euo pipefail

# ============================================================================
# Configuration
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SUBSET="${1:-all}"
REBUILD=0
for arg in "$@"; do
    case "${arg}" in
        --rebuild) REBUILD=1 ;;
    esac
done

# shellcheck source=scripts/shell/lib/install_helpers.sh
source "${SCRIPT_DIR}/shell/lib/install_helpers.sh"

LOCAL_IMAGE="hephaestus-ci:local"
GITLEAKS_IMAGE="ghcr.io/gitleaks/gitleaks:v8.30.0@sha256:691af3c7c5a48b16f187ce3446d5f194838f91238f27270ed36eef6359a574d9"

log_info()  { echo -e "${GREEN}[CI]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[CI]${NC} $*"; }
log_error() { echo -e "${RED}[CI]${NC} $*" >&2; }
log_step()  { echo -e "\n${BLUE}==>${NC} $*"; }

CANDIDATE_ROOT=""
CANDIDATE_TREE=""
CANDIDATE_INDEX_CONTAINER=""
CANDIDATE_OBJECTS_CONTAINER=""
REPOSITORY_OBJECTS_CONTAINER=""
CI_BUILD_ROOT=""
CI_RUN_IMAGE=""
CONTAINER_RUNNER_FAILURE_CODE=""

cleanup_candidate_snapshot() {
    if [ -z "${CANDIDATE_ROOT}" ]; then
        return
    fi
    case "${CANDIDATE_ROOT}" in
        "${PROJECT_ROOT}"/build/ci-candidate.*)
            rm -rf -- "${CANDIDATE_ROOT}"
            ;;
        *)
            log_error "Refusing to remove unexpected candidate path: ${CANDIDATE_ROOT}"
            ;;
    esac
    CANDIDATE_ROOT=""
    CANDIDATE_TREE=""
    CANDIDATE_INDEX_CONTAINER=""
    CANDIDATE_OBJECTS_CONTAINER=""
    REPOSITORY_OBJECTS_CONTAINER=""
}

cleanup_ci_build() {
    if [ -n "${CI_RUN_IMAGE}" ]; then
        case "${CI_RUN_IMAGE}" in
            hephaestus-ci:run-*)
                if ! "${CONTAINER_ENGINE}" image rm "${CI_RUN_IMAGE}" >/dev/null 2>&1; then
                    log_warn "Unable to remove temporary CI image tag: ${CI_RUN_IMAGE}"
                fi
                ;;
            *)
                log_error "Refusing to remove unexpected CI image tag: ${CI_RUN_IMAGE}"
                ;;
        esac
        CI_RUN_IMAGE=""
    fi
    if [ -n "${CI_BUILD_ROOT}" ]; then
        case "${CI_BUILD_ROOT}" in
            "${PROJECT_ROOT}"/build/ci-build.*)
                rm -rf -- "${CI_BUILD_ROOT}"
                ;;
            *)
                log_error "Refusing to remove unexpected CI build path: ${CI_BUILD_ROOT}"
                ;;
        esac
        CI_BUILD_ROOT=""
    fi
}

cleanup() {
    cleanup_candidate_snapshot
    cleanup_ci_build
}

prepare_candidate_snapshot() {
    local candidate_relative
    local candidate_sources
    local metadata
    local mode
    local path
    local repository_objects

    cleanup_candidate_snapshot
    if ! mkdir -p "${PROJECT_ROOT}/build"; then
        log_error "Unable to create the local CI candidate directory."
        return 1
    fi
    CANDIDATE_ROOT="$(mktemp -d "${PROJECT_ROOT}/build/ci-candidate.XXXXXX")" || {
        log_error "Unable to create the local CI candidate workspace."
        return 1
    }
    CANDIDATE_TREE="${CANDIDATE_ROOT}/tree"
    if ! mkdir -p "${CANDIDATE_TREE}" "${CANDIDATE_ROOT}/objects"; then
        log_error "Unable to create the local CI candidate workspace."
        return 1
    fi

    repository_objects="$(
        git -C "${PROJECT_ROOT}" rev-parse --path-format=absolute --git-path objects
    )" || {
        log_error "Unable to resolve repository Git objects."
        return 1
    }

    # Mirror the bytes that a later `git add -A` and commit would publish,
    # including non-ignored untracked files, without touching the real index or
    # object database.
    if ! GIT_INDEX_FILE="${CANDIDATE_ROOT}/index" \
        GIT_OBJECT_DIRECTORY="${CANDIDATE_ROOT}/objects" \
        GIT_ALTERNATE_OBJECT_DIRECTORIES="${repository_objects}" \
        git -C "${PROJECT_ROOT}" read-tree HEAD; then
        log_error "Unable to prepare the local CI candidate index."
        return 1
    fi
    if ! GIT_INDEX_FILE="${CANDIDATE_ROOT}/index" \
        GIT_OBJECT_DIRECTORY="${CANDIDATE_ROOT}/objects" \
        GIT_ALTERNATE_OBJECT_DIRECTORIES="${repository_objects}" \
        git -C "${PROJECT_ROOT}" add -A -- .; then
        log_error "Unable to stage the local CI candidate."
        return 1
    fi

    # The image allowlist must contain regular files only. checkout-index
    # preserves staged symlinks and ordinary cp would dereference their host
    # targets into the container build context before the later repository-wide
    # symlink gate runs. Reject symlinks and gitlinks at the alternate-index
    # boundary, before materializing or copying any candidate path.
    candidate_sources="${CANDIDATE_ROOT}/build-sources"
    if ! GIT_INDEX_FILE="${CANDIDATE_ROOT}/index" \
        GIT_OBJECT_DIRECTORY="${CANDIDATE_ROOT}/objects" \
        GIT_ALTERNATE_OBJECT_DIRECTORIES="${repository_objects}" \
        git -C "${PROJECT_ROOT}" ls-files --stage -z -- > "${candidate_sources}"; then
        log_error "Unable to inspect the local CI candidate."
        return 1
    fi
    while IFS=$'\t' read -r -d '' metadata path; do
        mode="${metadata%% *}"
        case "${path}" in
            ci|ci/Containerfile|uv.lock|pyproject.toml|.pre-commit-config.yaml|README.md|hephaestus|hephaestus/*)
                case "${mode}" in
                    100644|100755) ;;
                    *)
                        log_error "Candidate build source must be a regular file: ${path} (mode ${mode})"
                        return 1
                        ;;
                esac
                ;;
        esac
    done < "${candidate_sources}"
    if ! GIT_INDEX_FILE="${CANDIDATE_ROOT}/index" \
        GIT_OBJECT_DIRECTORY="${CANDIDATE_ROOT}/objects" \
        GIT_ALTERNATE_OBJECT_DIRECTORIES="${repository_objects}" \
        git -C "${PROJECT_ROOT}" \
        checkout-index --all --prefix="${CANDIDATE_TREE}/"; then
        log_error "Unable to materialize the local CI candidate."
        return 1
    fi

    candidate_relative="${CANDIDATE_ROOT#"${PROJECT_ROOT}/"}"
    CANDIDATE_INDEX_CONTAINER="/workspace/${candidate_relative}/index"
    CANDIDATE_OBJECTS_CONTAINER="/workspace/${candidate_relative}/objects"
    case "${repository_objects}" in
        "${PROJECT_ROOT}"/*)
            REPOSITORY_OBJECTS_CONTAINER="/workspace/${repository_objects#"${PROJECT_ROOT}/"}"
            ;;
        *)
            # Linked-worktree common metadata is mounted at its host path.
            REPOSITORY_OBJECTS_CONTAINER="${repository_objects}"
            ;;
    esac
}

trap cleanup EXIT

# ============================================================================
# Container engine detection
# ============================================================================

report_engine_failure() {
    CONTAINER_RUNNER_FAILURE_CODE="$1"
    log_error "$2"
    return 1
}

detect_engine() {
    if [ -n "${CONTAINER_ENGINE:-}" ]; then
        if ! command -v "${CONTAINER_ENGINE}" &> /dev/null; then
            report_engine_failure \
                "container-engine-absent" \
                "CONTAINER_ENGINE=${CONTAINER_ENGINE} not found in PATH"
            return 1
        fi
        if ! "${CONTAINER_ENGINE}" info >/dev/null 2>&1; then
            report_engine_failure \
                "container-engine-unavailable" \
                "CONTAINER_ENGINE=${CONTAINER_ENGINE} is unavailable. Start its service."
            return 1
        fi
        log_info "Container engine: ${CONTAINER_ENGINE} (from env)"
        return
    fi

    if command -v podman &> /dev/null && podman info >/dev/null 2>&1; then
        CONTAINER_ENGINE="podman"
        log_info "Container engine: podman (rootless)"
    elif command -v docker &> /dev/null && docker info >/dev/null 2>&1; then
        CONTAINER_ENGINE="docker"
        log_info "Container engine: docker"
    elif command -v podman &> /dev/null || command -v docker &> /dev/null; then
        report_engine_failure \
            "container-engine-unavailable" \
            "A container engine is installed but unavailable. Start its service."
        return 1
    else
        report_engine_failure \
            "container-engine-absent" \
            "No container engine found. Install podman (recommended) or docker."
        log_error "  Podman: https://podman.io/getting-started/installation"
        return 1
    fi
    export CONTAINER_ENGINE
}

# ============================================================================
# Image resolution
# ============================================================================

is_immutable_image_id() {
    [[ "$1" =~ ^(sha256:)?[0-9a-f]{64}$ ]]
}

build_ci_image() {
    local build_context
    local image_id_file
    local target_arch

    # BuildKit injects TARGETARCH automatically, but Docker's legacy builder
    # does not. Pass the normalized local architecture explicitly so the
    # pinned uv, gh, and just artifacts are selected on either builder.
    case "$(uname -m)" in
        x86_64|amd64) target_arch="amd64" ;;
        aarch64|arm64) target_arch="arm64" ;;
        *)
            log_error "Unsupported local CI build architecture: $(uname -m)"
            return 1
            ;;
    esac

    # Build from the exact publishable candidate bytes. The alternate index
    # excludes ignored local files without mutating the implementer's index.
    if ! prepare_candidate_snapshot; then
        return 1
    fi
    if ! mkdir -p "${PROJECT_ROOT}/build"; then
        log_error "Unable to create the local CI build directory."
        return 1
    fi
    CI_BUILD_ROOT="$(mktemp -d "${PROJECT_ROOT}/build/ci-build.XXXXXX")" || {
        log_error "Unable to create the local CI build workspace."
        return 1
    }
    build_context="${CI_BUILD_ROOT}/context"
    image_id_file="${CI_BUILD_ROOT}/image-id"
    CI_RUN_IMAGE="hephaestus-ci:run-$$-${RANDOM}"
    if ! mkdir -p "${build_context}/ci"; then
        log_error "Unable to create the local CI build context."
        return 1
    fi

    # Do not send the checkout as build context. In particular, ignored local
    # credentials, Git metadata, and unrelated build artifacts must never be
    # readable by the container engine. Keep this allowlist aligned with COPY
    # instructions in ci/Containerfile.
    if ! cp "${CANDIDATE_TREE}/ci/Containerfile" "${build_context}/ci/Containerfile"; then
        log_error "Unable to prepare the local CI build context."
        return 1
    fi
    if ! cp "${CANDIDATE_TREE}/uv.lock" \
        "${CANDIDATE_TREE}/pyproject.toml" \
        "${CANDIDATE_TREE}/.pre-commit-config.yaml" \
        "${CANDIDATE_TREE}/README.md" \
        "${build_context}/"; then
        log_error "Unable to prepare the local CI build context."
        return 1
    fi
    if ! cp -R "${CANDIDATE_TREE}/hephaestus" "${build_context}/hephaestus"; then
        log_error "Unable to prepare the local CI build context."
        return 1
    fi

    (
        cd "${build_context}"
        "${CONTAINER_ENGINE}" build \
            --build-arg "TARGETARCH=${target_arch}" \
            --iidfile "${image_id_file}" \
            -f ci/Containerfile \
            -t "${CI_RUN_IMAGE}" \
            -t "${LOCAL_IMAGE}" \
            .
    ) || {
        log_error "Failed to build local CI image '${LOCAL_IMAGE}'."
        return 1
    }
    CI_IMAGE="$(tr -d '\r\n' < "${image_id_file}")"
    if ! is_immutable_image_id "${CI_IMAGE}"; then
        log_error "Container engine returned an invalid image ID."
        return 1
    fi
    log_info "Built local CI image: ${CI_IMAGE}"
}

resolve_image_id() {
    CI_IMAGE="$(
        "${CONTAINER_ENGINE}" image inspect --format '{{.Id}}' "${LOCAL_IMAGE}"
    )" || {
        log_error "Unable to resolve immutable ID for '${LOCAL_IMAGE}'."
        return 1
    }
    CI_IMAGE="$(printf '%s' "${CI_IMAGE}" | tr -d '\r\n')"
    if ! is_immutable_image_id "${CI_IMAGE}"; then
        log_error "Container engine returned an invalid image ID."
        return 1
    fi
}

resolve_image() {
    if [ "${REBUILD}" = "1" ]; then
        log_info "Rebuilding CI image from the current checkout."
        if ! build_ci_image; then
            return 1
        fi
    elif "${CONTAINER_ENGINE}" image exists "${LOCAL_IMAGE}" 2>/dev/null || \
       "${CONTAINER_ENGINE}" images -q "${LOCAL_IMAGE}" 2>/dev/null | grep -q .; then
        if ! resolve_image_id; then
            return 1
        fi
        log_info "Using local CI image: ${CI_IMAGE}"
    else
        log_warn "Local image '${LOCAL_IMAGE}' not found; building it now."
        if ! build_ci_image; then
            return 1
        fi
    fi
    export CI_IMAGE
}

resolve_git_metadata_mount() {
    local common_dir
    common_dir="$(
        git -C "${PROJECT_ROOT}" rev-parse --path-format=absolute --git-common-dir
    )" || {
        log_error "Unable to resolve repository Git metadata."
        return 1
    }
    GIT_METADATA_MOUNT=()
    case "${common_dir}" in
        "${PROJECT_ROOT}"|"${PROJECT_ROOT}"/*)
            ;;
        *)
            # A linked worktree's .git file points into the primary checkout.
            # Preserve that absolute target inside the container, read-only, so
            # hatch-vcs, Git-aware tests, and scanners see the candidate commit.
            GIT_METADATA_MOUNT+=(--volume "${common_dir}:${common_dir}:ro")
            ;;
    esac
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
    local candidate_mount=()

    if [ "${CONTAINER_ENGINE}" = "podman" ]; then
        engine_flags+=("--userns=keep-id:uid=1000,gid=1000")
    else
        # Docker runs as the invoking host UID so bind-mounted artifacts retain
        # host ownership. Arbitrary UIDs cannot update the ci-owned baked venv,
        # so keep it read-only and import project code from the mounted checkout.
        engine_flags+=(--user "$(id -u):$(id -g)" --env HOME=/tmp \
            --env UV_NO_SYNC=1 --env PYTHONPATH=/workspace)
    fi

    if [ -n "${CANDIDATE_TREE}" ]; then
        candidate_mount+=(--volume "${CANDIDATE_TREE}:/candidate:ro")
    fi

    "${CONTAINER_ENGINE}" run --rm \
        "${engine_flags[@]}" \
        "${GIT_METADATA_MOUNT[@]}" \
        "${candidate_mount[@]}" \
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
    prepare_candidate_snapshot
    run_in_container env \
        "GIT_INDEX_FILE=${CANDIDATE_INDEX_CONTAINER}" \
        "GIT_OBJECT_DIRECTORY=${CANDIDATE_OBJECTS_CONTAINER}" \
        "GIT_ALTERNATE_OBJECT_DIRECTORIES=${REPOSITORY_OBJECTS_CONTAINER}" \
        uv run pre-commit run --all-files --show-diff-on-failure || return 1
    run_in_container uv run hephaestus-validate-links docs --repo-root . || return 1
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
    run_in_container bash -c '\
        uv run pytest tests/integration --require-cli --override-ini="addopts=" -v --strict-markers -m "not nightly and not artifact"'
}

run_cli() {
    log_step "Installed-CLI entry-point tests"
    # shellcheck disable=SC2016 # $PWD and WHEEL expand in the container shell.
    run_in_container bash -c '\
        uv build --wheel && \
        uv venv build/cli-venv && \
        WHEEL=(dist/*.whl) && \
        uv pip install --python build/cli-venv/bin/python "${WHEEL[0]}[automation]" pytest pyyaml && \
        export PATH="$PWD/build/cli-venv/bin:$PATH" && \
        build/cli-venv/bin/pytest tests/integration/test_cli_entry_points.py --require-cli \
            --override-ini="addopts=" -v --strict-markers'
}

run_build() {
    log_step "Reproducible artifact and package lifecycle validation"
    run_in_container uv run pytest tests/integration \
        --override-ini="addopts=" \
        --basetemp=build/pytest-artifacts \
        -v --strict-markers -m artifact
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
    run_in_container env UV_NO_SYNC=1 UV_OFFLINE=1 uv run check-jsonschema \
        --builtin-schema vendor.github-workflows \
        .github/workflows/*.yml
}

run_version() {
    log_step "Version single-source-of-truth + uv.lock check"
    run_in_container uv run python -m hephaestus.scripts_lib.check_version_single_source || return 1
    run_in_container uv lock --check || return 1
}

run_license() {
    log_step "License compatibility scan"
    run_in_container uv run python scripts/check_license_compatibility.py
}

run_license_blocking() {
    log_step "License compatibility scan (blocking PR mode)"
    run_in_container env GITHUB_EVENT_NAME=pull_request \
        uv run python scripts/check_license_compatibility.py
}

run_symlinks() {
    log_step "Repository symlink validation"
    run_in_container bash scripts/check-symlinks.sh
}

run_justfile() {
    log_step "Justfile evaluation"
    run_in_container just --evaluate >/dev/null || return 1
    run_in_container just --list >/dev/null
}

run_shellcheck() {
    log_step "ShellCheck"
    shopt -s nullglob globstar
    local files=(scripts/**/*.sh scripts/**/*.sbatch)
    if [ "${#files[@]}" -eq 0 ]; then
        log_info "No shell scripts found — nothing to lint."
        return 0
    fi
    run_in_container shellcheck --severity=error "${files[@]}"
}

run_shell_tests() {
    log_step "Bats shell tests"
    run_in_container bats --recursive tests/shell
}

run_secrets() {
    log_step "Gitleaks repository scan"
    local history_args=(detect --source=. --verbose --exit-code=1)
    local candidate_args=(dir --verbose --exit-code=1 .)
    prepare_candidate_snapshot
    if [ -f .gitleaks.toml ]; then
        history_args+=(--config=.gitleaks.toml)
        candidate_args+=(--config=.gitleaks.toml)
    fi
    "${CONTAINER_ENGINE}" run --rm \
        "${GIT_METADATA_MOUNT[@]}" \
        --volume "${PROJECT_ROOT}:/repo:Z" \
        --workdir /repo \
        "${GITLEAKS_IMAGE}" \
        "${history_args[@]}" || return 1
    "${CONTAINER_ENGINE}" run --rm \
        --volume "${CANDIDATE_TREE}:/candidate:ro" \
        --workdir /candidate \
        "${GITLEAKS_IMAGE}" \
        "${candidate_args[@]}"
}

# ============================================================================
# Main
# ============================================================================

FAILED=()

run_step() {
    local name="$1"
    local fn="$2"
    local status=0

    "${fn}" || status=$?
    if [ "${status}" -ne 0 ]; then
        FAILED+=("${name}")
        log_error "${name} FAILED"
    fi
}

prepare_container_runner() {
    if ! detect_engine; then
        return 1
    fi
    if ! resolve_image; then
        return 1
    fi
    if ! resolve_git_metadata_mount; then
        return 1
    fi
    if ! run_in_container true >/dev/null 2>&1; then
        report_engine_failure \
            "container-start-failed" \
            "The container engine could not start the local CI image."
        return 1
    fi
}

if ! prepare_container_runner; then
    if [ "${SUBSET}" = "all" ]; then
        case "${CONTAINER_RUNNER_FAILURE_CODE}" in
            container-engine-absent|container-engine-unavailable|container-start-failed)
                cleanup
                trap - EXIT
                printf '%s\n' \
                    "HEPHAESTUS_CI_RUNNER_FAILURE: ${CONTAINER_RUNNER_FAILURE_CODE}" >&2
                exit 75
                ;;
        esac
    fi
    exit 1
fi

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
    symlinks)
        run_step "symlinks" run_symlinks
        ;;
    justfile)
        run_step "justfile" run_justfile
        ;;
    shellcheck)
        run_step "shellcheck" run_shellcheck
        ;;
    shell-tests)
        run_step "shell-tests" run_shell_tests
        ;;
    secrets)
        run_step "secrets" run_secrets
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
        run_step "license" run_license_blocking
        run_step "symlinks" run_symlinks
        run_step "justfile" run_justfile
        run_step "shellcheck" run_shellcheck
        run_step "shell-tests" run_shell_tests
        run_step "secrets" run_secrets
        ;;
    *)
        log_error "Unknown subset: ${SUBSET}"
        log_error "Valid values: all, lint, unit, integration, cli, build, audit, sast, workflow-scan, schema, version, license, symlinks, justfile, shellcheck, shell-tests, secrets"
        exit 1
        ;;
esac

echo ""
if [ "${#FAILED[@]}" -eq 0 ]; then
    if [ "${SUBSET}" = "all" ]; then
        log_info "All locally executable CI checks passed."
    else
        log_info "Local CI subset '${SUBSET}' passed."
    fi
else
    log_error "Failed: ${FAILED[*]}"
    exit 1
fi
