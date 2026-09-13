"""Path-triggered host verification specifications for PR review."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _HostVerificationSpec:
    """One repository-owned verification command eligible for PR review."""

    changed_path: str | None
    argv: tuple[str, ...]
    descr: str
    additional_changed_paths: tuple[str, ...] = ()


_PATH_HOST_VERIFICATION_SPECS: tuple[_HostVerificationSpec, ...] = (
    _HostVerificationSpec(
        changed_path="docs/MIGRATION.md",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/docs/test_version_currency.py",
            "-q",
            "--tb=short",
        ),
        descr="review_migration_version_currency",
    ),
    _HostVerificationSpec(
        changed_path="tests/unit/automation/pipeline/test_worker_pool.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/automation/pipeline/test_worker_pool.py::TestAgentErrorHandling::test_codex_event_failure_is_explicit_agent_error",
            "-q",
            "--tb=short",
        ),
        descr="review_worker_pool_agent_execution_error",
    ),
    _HostVerificationSpec(
        changed_path="tests/unit/automation/pipeline/test_worker_pool.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/automation/pipeline/test_worker_pool.py::TestHostVerificationGitExecPath::test_active_sandbox_git_reads_validated_system_config",
            "-q",
            "--tb=short",
        ),
        descr="review_worker_pool_git_exec_path",
    ),
    _HostVerificationSpec(
        changed_path="tests/unit/automation/pipeline/test_worker_pool.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            (
                "tests/unit/automation/pipeline/test_worker_pool.py::"
                "TestWorkerPoolSubmitComplete::"
                "test_immutable_host_allows_descriptor_walk_to_scratch"
            ),
            "-q",
            "--tb=short",
        ),
        descr="review_worker_pool_scratch_descriptor_walk",
    ),
    _HostVerificationSpec(
        changed_path="tests/unit/automation/pipeline/test_worker_pool.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            (
                "tests/unit/automation/pipeline/test_worker_pool.py::"
                "TestWorkerPoolSubmitComplete::"
                "test_host_verification_profile_keeps_source_outside_writable_root"
            ),
            "-q",
            "--tb=short",
        ),
        descr="review_worker_pool_host_profile",
        additional_changed_paths=("hephaestus/automation/pipeline/worker_pool.py",),
    ),
    _HostVerificationSpec(
        changed_path="hephaestus/automation/pipeline/worker_pool.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/unit/automation/test_fleet_podman.py",
            "-q",
            "--tb=short",
        ),
        descr="review_fleet_podman_unix_socket",
    ),
    _HostVerificationSpec(
        changed_path="tests/performance/test_worker_pool_load.py",
        argv=(
            "uv",
            "run",
            "pytest",
            "-o",
            "addopts=",
            "tests/performance/test_worker_pool_load.py",
            "-q",
            "--load-report=../scratch/outputs/worker-pool.json",
        ),
        descr="review_stalled_consumer_verification",
    ),
)

__all__ = ["_PATH_HOST_VERIFICATION_SPECS", "_HostVerificationSpec"]
