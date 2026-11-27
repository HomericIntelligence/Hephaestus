"""Define failure classes that are safe for the durable event log."""

from __future__ import annotations

import logging
import math
import os
import re
import subprocess
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

import hephaestus.git.git_utils as git_utils
from hephaestus.agents.runtime import AgentExecutionError
from hephaestus.automation.pipeline.diagnostics import (
    _JOB_RESULT_ERROR_MAX as _ERR_MAX,
    _JOB_RESULT_TAIL as _TAIL,
    bounded_pipeline_diagnostic,
)
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.repository_lock import (
    LockMetadataError,
    LockTimeoutError,
    RepositoryLockError,
    _GitLockInterruptedError,
    _GitLockTimeoutError,
    _repository_lock_operation,
)
from hephaestus.automation.provider_auth_resilience import (
    is_provider_auth_error,
    provider_auth_error_message,
)
from hephaestus.automation.provider_transport_failures import (
    is_provider_transport_error,
    provider_transport_timeout_message,
)
from hephaestus.automation.review_journal import CommentJournalReadError
from hephaestus.git.gh.client import GitHubRateLimitError, GitHubUnavailableError
from hephaestus.git.source_worktree import (
    SourceWorkspaceError,
    SourceWorkspacePreparationCause,
    SourceWorkspacePreparationError,
)
from hephaestus.resilience import CircuitBreakerOpenError
from hephaestus.resilience.subprocess_resilience import is_transient_subprocess_error

if TYPE_CHECKING:
    from hephaestus.automation.pipeline.git_jobs import GitJob

# Keep existing worker log consumers on the same logger after this extraction.
logger = logging.getLogger("hephaestus.automation.pipeline.worker_pool")

_DURABLE_FAILURE_KINDS = frozenset(
    {
        "semantic_validation",
        "publish_remote_head_changed",
        "publish_remote_head_unchanged",
        "publish_remote_probe_failed",
        "publish_lease_drift",
        "publish_unknown",
        "publish_timeout",
        "publish_transport_failed",
        "github_rate_limit",
        "github_unavailable",
        "github_cli_error",
        "comment_journal_read_error",
        "validation",
        "validation_runner",
        "runner",
        "timeout",
        "lock_timeout",
        "lock_metadata_error",
        "source_workspace_ownership",
    }
)

_DURABLE_ERROR_CLASSES = {
    "circuit_open": "circuit_open",
    "lock_timeout": "lock_timeout",
    "lock_metadata_error": "lock_metadata_error",
    "review-session-lost": "session_lost",
}
_DURABLE_ERROR_PREFIXES = (
    ("source_workspace_ownership_unavailable:", "source_workspace_ownership_unavailable"),
    ("agent_error: codex_tool_or_provider_failure:", "codex_tool_or_provider_failure"),
    ("agent_error: provider_auth:", "provider_auth"),
    ("agent_error: provider_transport_timeout:", "provider_transport_timeout"),
    ("agent_error:", "agent_error"),
    ("parse failed:", "parse_error"),
    ("host_verification_", "host_verification"),
    ("mechanical rebase ", "git_operation"),
)
_PROCESS_EXIT_ERROR = re.compile(r"rc=-?\d+\Z")


def is_durable_failure_kind(value: object) -> bool:
    """Return whether a failure class is safe for the durable event log."""
    return isinstance(value, str) and value in _DURABLE_FAILURE_KINDS


def durable_error_class(error: str) -> str | None:
    """Return a safe class for one known worker error shape."""
    if error_class := _DURABLE_ERROR_CLASSES.get(error):
        return error_class
    if _PROCESS_EXIT_ERROR.fullmatch(error):
        return "process_exit"
    for prefix, error_class in _DURABLE_ERROR_PREFIXES:
        if error.startswith(prefix):
            return error_class
    return None


def repository_operation_lock_event_fields(value: object) -> dict[str, Any] | None:
    """Return one validated closed-schema repository-operation lock record."""
    keys = {
        "failure_kind",
        "repository",
        "waiting_operation",
        "waiting_process_id",
        "holder_operation",
        "holder_process_id",
        "holder_acquired_at",
        "holder_source",
        "wait_duration_s",
    }
    if not isinstance(value, dict) or set(value) != keys:
        return None
    failure_kind = value.get("failure_kind")
    repository = value.get("repository")
    waiting_operation = value.get("waiting_operation")
    waiting_process_id = value.get("waiting_process_id")
    holder_operation = value.get("holder_operation")
    holder_process_id = value.get("holder_process_id")
    holder_acquired_at = value.get("holder_acquired_at")
    holder_source = value.get("holder_source")
    wait_duration_s = value.get("wait_duration_s")
    if (
        failure_kind not in {"lock_timeout", "lock_metadata_error"}
        or not isinstance(repository, str)
        or not 0 < len(repository) <= 256
        or not isinstance(waiting_operation, str)
        or not 0 < len(waiting_operation) <= 200
        or isinstance(waiting_process_id, bool)
        or not isinstance(waiting_process_id, int)
        or waiting_process_id <= 0
        or isinstance(wait_duration_s, bool)
        or not isinstance(wait_duration_s, (int, float))
        or not math.isfinite(float(wait_duration_s))
        or float(wait_duration_s) < 0
        or holder_source
        not in {None, "in_process", "owner_sidecar", "owner_sentinel", "lock_metadata"}
    ):
        return None
    holder_values = (holder_operation, holder_process_id, holder_acquired_at)
    if failure_kind == "lock_timeout":
        if (
            not isinstance(holder_operation, str)
            or not 0 < len(holder_operation) <= 200
            or isinstance(holder_process_id, bool)
            or not isinstance(holder_process_id, int)
            or holder_process_id <= 0
            or not isinstance(holder_acquired_at, str)
            or not 0 < len(holder_acquired_at) <= 64
            or holder_source not in {"in_process", "owner_sidecar"}
        ):
            return None
    elif any(candidate is not None for candidate in holder_values) or holder_source not in {
        "in_process",
        "owner_sidecar",
        "owner_sentinel",
        "lock_metadata",
    }:
        return None
    return {
        "failure_kind": failure_kind,
        "repository": repository,
        "waiting_operation": waiting_operation,
        "waiting_process_id": waiting_process_id,
        "holder_operation": holder_operation,
        "holder_process_id": holder_process_id,
        "holder_acquired_at": holder_acquired_at,
        "holder_source": holder_source,
        "wait_duration_s": round(float(wait_duration_s), 3),
    }


def _bounded_lock_holder(value: object) -> dict[str, Any] | None:
    """Return validated checkout-holder fields for one event record."""
    if not isinstance(value, dict):
        return None
    holder: dict[str, Any] = {}
    pid = value.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        holder["pid"] = pid
    for key, limit in (
        ("run_identity", 128),
        ("repository", 256),
        ("operation", 128),
    ):
        candidate = value.get(key)
        if isinstance(candidate, str) and 0 < len(candidate) <= limit:
            holder[key] = candidate
    acquired_at = value.get("acquired_at_unix_s")
    if (
        isinstance(acquired_at, (int, float))
        and not isinstance(acquired_at, bool)
        and math.isfinite(float(acquired_at))
    ):
        holder["acquired_at_unix_s"] = float(acquired_at)
    return holder or None


def repository_contention_event_fields(value: object) -> dict[str, Any] | None:
    """Return bounded checkout-contention fields for one event record."""
    if not isinstance(value, dict):
        return None
    contention: dict[str, Any] = {}
    for key, limit in (
        ("repository", 256),
        ("operation", 128),
        ("lock_layer", 32),
        ("lock_path", 1024),
        ("run_identity", 128),
        ("holder_metadata_status", 32),
    ):
        candidate = value.get(key)
        if isinstance(candidate, str) and len(candidate) <= limit:
            if key == "lock_layer" and candidate not in {"in_process", "advisory", "owner"}:
                continue
            if key == "holder_metadata_status" and candidate not in {
                "unavailable",
                "unverified",
                "verified",
            }:
                continue
            contention[key] = candidate
    for key in ("configured_lock_wait_s", "attempt_wait_s"):
        candidate = value.get(key)
        if (
            candidate is None
            or isinstance(candidate, bool)
            or not isinstance(candidate, (int, float))
            or not math.isfinite(float(candidate))
            or float(candidate) < 0
        ):
            continue
        contention[key] = round(float(candidate), 3)
    if isinstance(value.get("holder_metadata_advisory"), bool):
        contention["holder_metadata_advisory"] = value["holder_metadata_advisory"]
    if value.get("holder_metadata_stale") is True:
        contention["holder_metadata_stale"] = True
    holder = _bounded_lock_holder(value.get("holder_metadata"))
    if holder is not None:
        contention["holder_metadata"] = holder
    return contention or None


def agent_failure_diagnostic(stderr_tail: str) -> str | None:
    """Classify agent stderr for logs without exposing subprocess output."""
    text = stderr_tail.strip().casefold()
    if not text:
        return None
    if "stream disconnected" in text or "error sending request" in text:
        return "backend response stream disconnected"
    if "rate limit" in text or "usage cap" in text or "429" in text:
        return "provider rate limit"
    if "connection" in text or "network" in text or "timed out" in text:
        return "transient transport failure"
    return "agent subprocess reported diagnostic output"


def agent_failure_reason(error: str | None) -> str:
    """Render an agent failure reason for logs without exposing error text."""
    if error == "timeout":
        return "agent timed out"
    if error == "circuit_open":
        return "agent circuit is open"
    if error == "interrupted_before_start":
        return "agent job was interrupted before start"
    if error is not None and error.startswith("rc="):
        returncode = error.removeprefix("rc=")
        if returncode.lstrip("-").isdigit() and returncode != "":
            return f"exit code {returncode}"
    return "agent job failed"


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    """Return a finite causal chain in command execution order."""
    ordered: list[BaseException] = []
    seen: set[int] = set()

    def visit(current: BaseException | None) -> None:
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        cause = current.__cause__
        context = None if current.__suppress_context__ else current.__context__
        if context is not cause:
            visit(context)
        visit(cause)
        ordered.append(current)

    visit(exc)
    return tuple(ordered)


def _git_exception_diagnostics(
    exc: BaseException,
) -> tuple[int | None, str, str, str]:
    """Return the decisive class, return code, and safe ordered streams."""
    failures = tuple(
        current
        for current in _exception_chain(exc)
        if isinstance(current, (subprocess.CalledProcessError, subprocess.TimeoutExpired))
    )
    if not failures:
        return None, type(exc).__name__, "", ""
    decisive = failures[-1]
    returncode = (
        decisive.returncode if isinstance(decisive, subprocess.CalledProcessError) else None
    )

    def streams(attribute: str) -> str:
        limit = max(1, _TAIL // len(failures))
        parts: list[str] = []
        for failure in failures:
            value = getattr(failure, attribute, None)
            if attribute == "stdout" and value is None:
                value = getattr(failure, "output", None)
            text = bounded_pipeline_diagnostic(value, limit=limit)
            if text:
                parts.append(text)
        return bounded_pipeline_diagnostic("\n".join(parts), limit=_TAIL)

    return returncode, type(decisive).__name__, streams("stdout"), streams("stderr")


def _publication_failure_diagnostic(
    exc: BaseException,
    *,
    phase: str,
    remote_state: str,
    head_sha: str | None,
) -> tuple[dict[str, object], str, str]:
    """Build one bounded diagnostic sidecar for a failed publication."""
    returncode, exception_class, stdout_tail, stderr_tail = _git_exception_diagnostics(exc)
    diagnostic: dict[str, object] = {
        "failure_kind": "publication",
        "phase": phase,
        "head_sha": head_sha,
        "exception_class": exception_class,
        "remote_state": remote_state,
    }
    if returncode is not None:
        diagnostic["returncode"] = returncode
    return diagnostic, stdout_tail, stderr_tail


def _publication_failure_location(exc: BaseException) -> tuple[str, str]:
    """Return the phase and remote state for a publication failure."""
    if isinstance(exc, git_utils.BranchPublicationRemoteHeadChangedError):
        return "push", "changed"
    if isinstance(exc, git_utils.BranchPublicationRemoteHeadUnchangedError):
        return "push", "unchanged"
    if isinstance(exc, git_utils.BranchPublicationRemoteProbeError):
        return "remote_probe", "unverified"
    return "push", "unverified"


def _ordered_git_diagnostics(*values: object) -> str:
    """Return non-empty diagnostics in input order within one fixed bound."""
    nonempty = [value for value in values if value]
    if not nonempty:
        return ""
    per_value_limit = max(1, _TAIL // len(nonempty))
    return bounded_pipeline_diagnostic(
        "\n".join(bounded_pipeline_diagnostic(value, limit=per_value_limit) for value in nonempty),
        limit=_TAIL,
    )


def _retain_publication_failure(result: JobResult, prior: JobResult | None) -> JobResult:
    """Keep earlier push evidence without replacing the later outcome or cause."""
    if prior is None:
        return result
    return replace(
        result,
        stdout_tail=_ordered_git_diagnostics(prior.stdout_tail, result.stdout_tail),
        stderr_tail=_ordered_git_diagnostics(prior.stderr_tail, result.stderr_tail),
        process_failure=result.process_failure or prior.process_failure,
    )


def _classify_github_failure(error: BaseException, *, now_epoch: float) -> tuple[str, float] | None:
    """Return a safe GitHub failure class and its first retry delay."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and all(current is not seen for seen in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    rate_error = next((item for item in chain if isinstance(item, GitHubRateLimitError)), None)
    breaker_error = next(
        (item for item in chain if isinstance(item, CircuitBreakerOpenError)), None
    )
    if isinstance(rate_error, GitHubRateLimitError):
        delay = (
            max(0.0, float(rate_error.reset_epoch) - now_epoch)
            if rate_error.reset_epoch > 0
            else 60.0
        )
        return "github_rate_limit", delay
    if any(isinstance(item, GitHubUnavailableError) for item in chain) or isinstance(
        breaker_error, CircuitBreakerOpenError
    ):
        delay = (
            max(0.0, breaker_error.time_until_recovery)
            if isinstance(breaker_error, CircuitBreakerOpenError)
            else 60.0
        )
        return "github_unavailable", delay
    if any(isinstance(item, subprocess.CalledProcessError) for item in chain):
        return "github_cli_error", 1.0
    if any(isinstance(item, (subprocess.TimeoutExpired, _GitLockTimeoutError)) for item in chain):
        return "github_timeout", 1.0
    if any(isinstance(item, CommentJournalReadError) for item in chain):
        return "comment_journal_read_error", 1.0
    return None


def _agent_exception_result(exc: Exception) -> JobResult:
    """Map provider-declared and unexpected agent exceptions to job results."""
    if isinstance(exc, AgentExecutionError):
        detail = str(exc)
        if is_provider_auth_error(detail):
            return JobResult(
                ok=False,
                error=provider_auth_error_message(detail),
            )
        if is_provider_transport_error(detail):
            return JobResult(
                ok=False,
                error=provider_transport_timeout_message(detail),
            )
        return JobResult(
            ok=False,
            error=f"agent_error: {exc!s}"[:_ERR_MAX],
        )
    logger.exception("Agent job raised, returning error result")
    pre_agent_failure: (
        Literal["source_workspace_preparation", "source_workspace_ownership"] | None
    ) = None
    if isinstance(exc, SourceWorkspacePreparationError):
        pre_agent_failure = "source_workspace_preparation"
    elif isinstance(exc, SourceWorkspaceError):
        pre_agent_failure = "source_workspace_ownership"
    return JobResult(
        ok=False,
        error=f"{type(exc).__name__}: {exc!s}"[:_ERR_MAX],
        pre_agent_failure=pre_agent_failure,
    )


class _RebaseSigningEnvironmentError(RuntimeError):
    """Raised when a policy rebase cannot obtain the validated signing bridge."""


class _RemoteGitAuthenticationError(RuntimeError):
    """Raised when a remote Git operation cannot use trusted authentication."""


def _ignore_local_agent_failure(error: BaseException) -> bool:
    """Count provider transport failures and ignore deterministic local errors."""
    if isinstance(error, ConnectionError):
        return False
    if isinstance(error, AgentExecutionError):
        if is_provider_transport_error(str(error)):
            return False
        if error.__cause__ is not None:
            error = error.__cause__
    return not is_transient_subprocess_error(error)


def _git_lock_failure_result(
    exc: RepositoryLockError | _GitLockTimeoutError | _GitLockInterruptedError,
    *,
    job: GitJob | None = None,
    attempt_started_s: float | None = None,
) -> JobResult:
    """Map a typed Git-lock failure to the corresponding bounded job result."""
    if isinstance(exc, RepositoryLockError):
        details = dict(exc.details)
        if job is not None:
            details["repository"] = job.repo
            details["waiting_operation"] = _repository_lock_operation(job.descr, job.op)
        if isinstance(exc, (LockTimeoutError, LockMetadataError)):
            return JobResult(ok=False, error=exc.failure_kind, value=details)
        return JobResult(
            ok=False,
            interrupted=True,
            error="interrupted_waiting_for_git_lock",
            value=details,
        )
    if isinstance(exc, _GitLockTimeoutError):
        if job is None or attempt_started_s is None:
            return JobResult(ok=False, error="lock_metadata_error")
        value: dict[str, object] = {
            "failure_kind": "lock_metadata_error",
            "repository": job.repo,
            "waiting_operation": _repository_lock_operation(job.descr, job.op),
            "waiting_process_id": os.getpid(),
            "holder_operation": None,
            "holder_process_id": None,
            "holder_acquired_at": None,
            "holder_source": "lock_metadata",
            "wait_duration_s": round(max(time.monotonic() - attempt_started_s, 0.0), 3),
        }
        return JobResult(ok=False, error="lock_metadata_error", value=value)
    return JobResult(
        ok=False,
        interrupted=True,
        error="interrupted_waiting_for_git_lock",
    )


def _dirty_direct_failure_result(
    exc: Exception,
    *,
    fallback_error: str,
    fallback_value: dict[str, object],
    timeout_value: dict[str, object] | None = None,
) -> JobResult:
    """Map one dirty writer timeout and preserve the operation failure contract."""
    if (
        isinstance(exc, SourceWorkspacePreparationError)
        and exc.cause is SourceWorkspacePreparationCause.GIT_TIMEOUT
    ):
        logger.warning(
            "dirty direct continuation was not claimed: operation deadline hit "
            "before the claim could complete",
        )
        return JobResult(ok=False, error="timeout", value=timeout_value)
    logger.warning("dirty direct continuation was not claimed: %s", exc, exc_info=True)
    return JobResult(
        ok=False,
        error=fallback_error,
        value=fallback_value,
    )


def _git_environment_failure_result(
    exc: _RebaseSigningEnvironmentError | _RemoteGitAuthenticationError,
) -> JobResult:
    """Map a controlled Git environment failure to a typed job result."""
    failure_kind = (
        "signing_configuration"
        if isinstance(exc, _RebaseSigningEnvironmentError)
        else "remote_authentication"
    )
    return JobResult(ok=False, error=str(exc), value={"failure_kind": failure_kind})
