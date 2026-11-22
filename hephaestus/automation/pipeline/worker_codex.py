"""Build and validate isolated Codex implementation requests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

import hephaestus.agents.runtime as agent_runtime
import hephaestus.automation.codex_adapter_admission as codex_adapter_admission
import hephaestus.automation.pipeline.codex_worktree_boundary as codex_worktree_boundary
from hephaestus.agents.codex_isolation import (
    CodexExecutionPolicyV1,
    CodexGitReceiptV1,
    CodexIsolationAdapterV1,
    CodexIsolationError,
    CodexIsolationRequestV1,
    StagedLinuxExecutable,
    canonical_sha256,
    new_run_nonce,
)
from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionPolicyError,
    FilesystemMode,
    SessionLifecycle,
    resolve_policy,
)
from hephaestus.agents.model_selection import AgentModelSelection, resolve_codex_model_selection
from hephaestus.config.child_environments import build_codex_implementation_child_env

if TYPE_CHECKING:
    from hephaestus.automation.pipeline.jobs import AgentJob


_CODEX_IMPLEMENTATION_OUTPUT_MAX_BYTES = 1024 * 1024


_CODEX_IMPLEMENTATION_GRACE_SECONDS = 5.0


_CODEX_IMPLEMENTATION_INVENTORY_QUIESCENCE_SECONDS = 1.0


_CODEX_IMPLEMENTATION_PROVIDER_RELAY = "vsock://2:443"


def _uses_codex_implementation_adapter(job: AgentJob) -> bool:
    """Return true for a Codex implementation job with an adapter selection."""
    return bool(
        agent_runtime.requires_codex_implementation_isolation(job.agent)
        and job.execution_request is not None
        and job.execution_request.role is AgentRole.IMPLEMENTER
        and any(
            value is not None
            for value in (
                job.codex_isolation_adapter,
                job.codex_isolation_deployment_lock,
                job.codex_isolation_deployment_lock_sha256,
                job.codex_isolation_request,
            )
        )
    )


@contextmanager
def _codex_git_boundary(cwd: Path) -> Iterator[codex_worktree_boundary.CodexWorktreeBoundary]:
    """Map Git receipt failures to one stable adapter error."""
    try:
        with codex_worktree_boundary.capture_codex_worktree_boundary(cwd) as boundary:
            yield boundary
    except codex_worktree_boundary.CodexWorktreeBoundaryError:
        raise CodexIsolationError("codex_adapter_request_mismatch") from None


def _codex_implementation_command(
    *,
    executable: Path,
    worktree: Path,
    model: str,
    session_id: str | None,
    sandbox: str,
    operation: AgentOperation,
    allowed_tools: tuple[str, ...],
) -> tuple[str, ...]:
    """Build one exact guest command from frozen worker inputs."""
    command = [str(executable), "exec"]
    if session_id:
        command.extend(("resume", session_id))
    selection = resolve_codex_model_selection(model)
    if selection.model:
        command.extend(("--model", selection.model))
    if selection.reasoning_effort not in {"", "default"}:
        command.extend(("-c", f"model_reasoning_effort={json.dumps(selection.reasoning_effort)}"))
    if session_id:
        command.extend(
            (
                "-c",
                f"sandbox_mode={json.dumps(sandbox)}",
                "-c",
                'approval_policy="never"',
            )
        )
    else:
        command.extend(
            (
                "--cd",
                str(worktree),
                "--sandbox",
                sandbox,
                "-c",
                'approval_policy="never"',
            )
        )
    command.extend(
        (
            "-c",
            f"hephaestus_automation.operation={json.dumps(operation.value)}",
            "-c",
            "hephaestus_automation.allowed_tools="
            + json.dumps(list(allowed_tools), separators=(",", ":")),
            "--json",
            "-",
        )
    )
    return tuple(command)


_CODEX_TOOL_CAPABILITIES = {
    "Bash": "bash",
    "Edit": "edit",
    "Glob": "find",
    "Grep": "grep",
    "Read": "read",
    "Write": "write",
}


_CODEX_NON_APPLICABLE_TOOLS = {
    AgentOperation.ADDRESS_REVIEW: frozenset({"Skill", "Task"}),
}


_CODEX_OPERATION_TOOLS = {
    AgentOperation.IMPLEMENT_INSPECT: ("Glob", "Grep", "Read"),
    AgentOperation.IMPLEMENT: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
    AgentOperation.REBASE_CONFLICT: ("Edit", "Glob", "Grep", "Read", "Write"),
    AgentOperation.TEST_FIX: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
    AgentOperation.ADDRESS_REVIEW: ("Bash", "Edit", "Glob", "Grep", "Read", "Write"),
}


def _codex_implementation_grants(job: AgentJob) -> tuple[str, tuple[str, ...], bool]:
    """Resolve and validate the operation-specific Codex grants."""
    execution = job.execution_request
    if execution is None or execution.role is not AgentRole.IMPLEMENTER:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    try:
        operation_policy = resolve_policy(execution)
    except ExecutionPolicyError:
        raise CodexIsolationError("codex_adapter_request_mismatch") from None
    workspace_write = operation_policy.filesystem is FilesystemMode.WORKTREE_RW
    sandbox = "workspace-write" if workspace_write else "read-only"
    if job.sandbox != sandbox:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    expected_tools = _CODEX_OPERATION_TOOLS.get(execution.operation)
    if expected_tools is None:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    declared_tools = {
        value.strip()
        for value in (job.allowed_tools or ",".join(expected_tools)).split(",")
        if value.strip()
    }
    non_applicable = _CODEX_NON_APPLICABLE_TOOLS.get(execution.operation, frozenset())
    allowed_tools = tuple(sorted(declared_tools - non_applicable))
    capabilities = {_CODEX_TOOL_CAPABILITIES.get(value, "") for value in allowed_tools}
    if (
        allowed_tools != expected_tools
        or "" in capabilities
        or not capabilities <= operation_policy.builtins
    ):
        raise CodexIsolationError("codex_adapter_request_mismatch")
    return sandbox, allowed_tools, workspace_write


def _reject_codex_terminal_cleanup_tombstone(profiles: Path) -> None:
    """Reject a session store while terminal cleanup is incomplete."""
    terminal_cleanup = profiles.with_name(profiles.name + ".terminal-cleanup")
    try:
        terminal_cleanup.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise CodexIsolationError("codex_adapter_inventory_uncertain") from None
    raise CodexIsolationError("codex_adapter_inventory_uncertain")


def _codex_private_profile(job: AgentJob, build_root: Path) -> Path:
    """Return one durable profile that is bound to the issue and cycle."""
    logical_session = job.session_key or job.session_agent
    if not logical_session:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    identity = canonical_sha256(
        (job.repo, int(job.issue), logical_session, str(job.cwd.resolve(strict=True)), job.model)
    )
    worktree = job.cwd.resolve(strict=True)
    profiles = worktree.parent / f".{worktree.name}-codex-sessions"
    _reject_codex_terminal_cleanup_tombstone(profiles)
    descriptor = -1
    try:
        with suppress(FileExistsError):
            profiles.mkdir(mode=0o700)
        lexical = profiles.lstat()
        descriptor = os.open(
            profiles,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(lexical.st_mode)
            or lexical.st_uid != os.geteuid()
            or (lexical.st_dev, lexical.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("invalid Codex profile root")
        os.fchmod(descriptor, 0o700)
        canonical_profiles = profiles.resolve(strict=True)
        if canonical_profiles != profiles.absolute():
            raise OSError("Codex profile root is not canonical")
    except OSError:
        raise CodexIsolationError("codex_adapter_protocol_mismatch") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if canonical_profiles.is_relative_to(worktree) or not build_root.is_relative_to(worktree):
        raise CodexIsolationError("codex_adapter_protocol_mismatch")
    profile = canonical_profiles / identity
    if profile.is_symlink():
        raise CodexIsolationError("codex_adapter_protocol_mismatch")
    receipt_paths = (profile / ".active.json", profile / ".quarantine.json")
    if any(path.exists() or path.is_symlink() for path in receipt_paths):
        raise CodexIsolationError("codex_adapter_inventory_uncertain")
    try:
        with suppress(FileExistsError):
            profile.mkdir(mode=0o700)
        profile_status = profile.lstat()
        if (
            not stat.S_ISDIR(profile_status.st_mode)
            or profile_status.st_uid != os.geteuid()
            or stat.S_IMODE(profile_status.st_mode) != 0o700
        ):
            raise OSError("invalid Codex durable store")
        runs = profile / ".runs"
        with suppress(FileExistsError):
            runs.mkdir(mode=0o700)
        runs_status = runs.lstat()
        if (
            not stat.S_ISDIR(runs_status.st_mode)
            or runs_status.st_uid != os.geteuid()
            or stat.S_IMODE(runs_status.st_mode) != 0o700
        ):
            raise OSError("invalid Codex run store")
    except OSError:
        raise CodexIsolationError("codex_adapter_protocol_mismatch") from None
    return profile


def _initialize_codex_adapter(
    admission: codex_adapter_admission.CodexAdapterAdmission,
) -> CodexIsolationAdapterV1:
    """Initialize one admitted adapter and check its locked identity."""
    factory = admission.factory
    if (
        not callable(factory)
        or getattr(factory, "codex_isolation_api_version", None)
        != admission.lock.adapter_api_version
    ):
        raise CodexIsolationError("codex_adapter_protocol_mismatch")
    try:
        adapter = factory()
    except CodexIsolationError:
        raise
    except BaseException:
        raise CodexIsolationError("codex_adapter_initialization_failed") from None
    return cast(CodexIsolationAdapterV1, adapter)


@contextmanager
def _owned_codex_adapter(
    admission: codex_adapter_admission.CodexAdapterAdmission,
) -> Iterator[CodexIsolationAdapterV1]:
    """Close one admitted helper on all initialization and execution paths."""
    adapter: CodexIsolationAdapterV1 | None = None
    try:
        adapter = _initialize_codex_adapter(admission)
        yield adapter
    finally:
        owner = adapter if adapter is not None else admission.factory
        close_adapter = getattr(owner, "_close", None)
        if callable(close_adapter):
            close_adapter()


def _validate_staged_codex_executable(executable: StagedLinuxExecutable) -> None:
    """Recheck the staged path identity and bytes without following a link."""
    descriptor = -1
    try:
        descriptor = os.open(
            executable.path,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        value = os.fstat(descriptor)
        identity = (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_size,
            value.st_mtime_ns,
        )
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
        if identity != executable.file_identity or digest.hexdigest() != executable.digest:
            raise CodexIsolationError("codex_adapter_request_mismatch")
    except CodexIsolationError:
        raise
    except OSError:
        raise CodexIsolationError("codex_adapter_request_mismatch") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _run_isolated_codex_effort_attempts(
    execute: Callable[[str], agent_runtime.AgentRunResult], model: str
) -> agent_runtime.AgentRunResult:
    """Retry one verified pre-work effort failure through the admitted boundary."""
    selection = resolve_codex_model_selection(model)
    try:
        return execute(selection.reference)
    except agent_runtime._CodexReasoningEffortRejectedError:
        if selection.reasoning_effort in {"", "default"}:
            raise
    return execute(AgentModelSelection(selection.model, "default"))


def _codex_implementation_request(
    *,
    job: AgentJob,
    worktree: Path,
    prompt: str,
    private_profile: Path,
    admission: codex_adapter_admission.CodexAdapterAdmission,
    git_receipt: CodexGitReceiptV1,
    executable: StagedLinuxExecutable,
    model_reference: str | None = None,
    deadline_s: float | None = None,
) -> CodexIsolationRequestV1:
    """Build the complete frozen request for one admitted adapter."""
    lock = admission.lock
    execution = job.execution_request
    if execution is None:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    sandbox, allowed_tools, workspace_write = _codex_implementation_grants(job)
    if executable.digest != lock.extracted_elf_sha256:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    run_nonce = new_run_nonce()
    ephemeral_profile = private_profile / ".runs" / run_nonce / "profile"
    fixed_git_environment = dict(git_receipt.fixed_environment)
    environment = tuple(
        sorted(
            build_codex_implementation_child_env(
                codex_home=ephemeral_profile,
                fixed_git_environment=fixed_git_environment,
            ).items()
        )
    )
    if execution.lifecycle is SessionLifecycle.RESUME_REQUIRED:
        if job.resume_binding is not None:
            raise CodexIsolationError("codex_adapter_request_mismatch")
        session_id = job.resume_session_id
        if not session_id:
            raise CodexIsolationError("codex_adapter_request_mismatch")
    elif job.resume_binding is not None or job.resume_session_id is not None:
        raise CodexIsolationError("codex_adapter_request_mismatch")
    else:
        session_id = None
    session = json.dumps(
        {
            "allowed_tools": list(allowed_tools),
            "lifecycle": execution.lifecycle.value,
            "operation": execution.operation.value,
            "session_id": session_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    command = _codex_implementation_command(
        executable=executable.path,
        worktree=worktree,
        model=job.model if model_reference is None else model_reference,
        session_id=session_id,
        sandbox=sandbox,
        operation=execution.operation,
        allowed_tools=allowed_tools,
    )
    profile_read_only, profile_read_write = agent_runtime._codex_profile_policy_paths(
        ephemeral_profile,
        run_nonce,
    )
    read_only_mounts = {
        *git_receipt.read_only_paths,
        str(executable.path),
        *profile_read_only,
    }
    read_write_mounts = set(profile_read_write)
    if workspace_write:
        read_write_mounts.update(git_receipt.read_write_paths)
    else:
        read_only_mounts.update(git_receipt.read_write_paths)
    policy = CodexExecutionPolicyV1(
        schema_version=1,
        read_only_mounts=tuple(sorted(read_only_mounts)),
        read_write_mounts=tuple(sorted(read_write_mounts)),
        protected_overlay_mounts=tuple(sorted(git_receipt.protected_paths)),
        provider_relay=_CODEX_IMPLEMENTATION_PROVIDER_RELAY,
        command_network="deny",
        max_output_bytes=_CODEX_IMPLEMENTATION_OUTPUT_MAX_BYTES,
        term_grace_seconds=_CODEX_IMPLEMENTATION_GRACE_SECONDS,
        kill_grace_seconds=_CODEX_IMPLEMENTATION_GRACE_SECONDS,
        pipe_close_grace_seconds=_CODEX_IMPLEMENTATION_GRACE_SECONDS,
        inventory_quiescence_seconds=_CODEX_IMPLEMENTATION_INVENTORY_QUIESCENCE_SECONDS,
        total_deadline=float(job.timeout_s),
    )
    worktree_identity = git_receipt.canonical_worktree
    model = job.model
    issue = int(job.issue)
    session_identity = (
        job.repo,
        issue,
        "implementer",
        worktree_identity,
        model,
        session,
    )
    return CodexIsolationRequestV1(
        schema_version=1,
        run_nonce=run_nonce,
        entry_point_name=lock.entry_point_name,
        adapter_api_version=lock.adapter_api_version,
        package_version=lock.adapter_version,
        deployment_lock_digest=admission.deployment_lock_sha256,
        wheel_digest=lock.wheel_sha256,
        installed_tree_digest=lock.installed_tree_sha256,
        command=command,
        command_digest=canonical_sha256(command),
        executable_platform="linux",
        executable_target=lock.codex_target,
        executable_release=lock.codex_release_tag,
        executable_asset_name=lock.codex_archive_asset,
        executable_path=str(executable.path),
        executable_digest=executable.digest,
        executable_file_identity=executable.file_identity,
        guest_image_digest=lock.guest_image_sha256,
        environment=environment,
        environment_digest=canonical_sha256(environment),
        prompt=prompt,
        prompt_digest=canonical_sha256(prompt),
        worktree_path=str(worktree),
        private_profile_path=str(ephemeral_profile),
        policy=policy,
        policy_digest=canonical_sha256(policy),
        git_receipt=git_receipt,
        git_receipt_digest=canonical_sha256(git_receipt),
        repository=job.repo,
        issue=issue,
        role="implementer",
        worktree_identity=worktree_identity,
        model=model,
        session=session,
        session_identity_digest=canonical_sha256(session_identity),
        monotonic_deadline=(time.monotonic() + job.timeout_s if deadline_s is None else deadline_s),
    )
