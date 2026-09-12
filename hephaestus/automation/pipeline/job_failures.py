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
        "repository_busy",
        "source_workspace_ownership",
    }
)

_DURABLE_ERROR_CLASSES = {
    "circuit_open": "circuit_open",
    "lock_timeout": "lock_timeout",
    "repository_busy": "repository_busy",
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


def _bounded_lock_holder(value: object) -> dict[str, Any] | None:
    """Return validated holder fields that are safe for one event record."""
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
    """Return validated checkout-contention fields for one event record."""
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
            if key == "lock_layer" and candidate not in {"in_process", "advisory"}:
                continue
            if key == "holder_metadata_status" and candidate not in {
                "unavailable",
                "unverified",
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
    if value.get("holder_metadata_advisory") is True:
        contention["holder_metadata_advisory"] = True
    if value.get("holder_metadata_stale") is True:
        contention["holder_metadata_stale"] = True
    holder = _bounded_lock_holder(value.get("holder_metadata"))
    if holder is not None:
        contention["holder_metadata"] = holder
    return contention or None
