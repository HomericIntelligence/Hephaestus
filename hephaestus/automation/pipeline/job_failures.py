"""Define failure classes that are safe for the durable event log."""

from __future__ import annotations

import math
import re
from typing import Any

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
