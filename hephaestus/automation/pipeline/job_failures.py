"""Define failure classes that are safe for the durable event log."""

from __future__ import annotations

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


def is_durable_failure_kind(value: object) -> bool:
    """Return whether a failure class is safe for the durable event log."""
    return isinstance(value, str) and value in _DURABLE_FAILURE_KINDS
