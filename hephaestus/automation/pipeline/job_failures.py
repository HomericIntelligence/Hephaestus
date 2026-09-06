"""Define failure classes that are safe for the durable event log."""

from __future__ import annotations

import re

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
    }
)

_DURABLE_ERROR_CLASSES = {
    "circuit_open": "circuit_open",
    "review-session-lost": "session_lost",
}
_DURABLE_ERROR_PREFIXES = (
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
