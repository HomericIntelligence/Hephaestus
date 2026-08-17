"""Pure helpers for redacting and bounding durable diagnostic text."""

from __future__ import annotations

import re

_DEFAULT_DIAGNOSTIC_LIMIT = 2000
_REDACTED_GIT_URL = "<redacted-git-url>"
_REDACTED_VALUE = "<redacted-value>"
_GIT_URL_RE = re.compile(r"\b(?:https?|ssh|git)://\S+", re.IGNORECASE)
_GIT_SCP_REMOTE_RE = re.compile(r"(?<![\w./-])(?:[\w.-]+@)?[\w.-]+:\S+(?:\.git)?")
_GIT_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(access_token|auth_token|oauth_token|token|password|passwd|secret|credential)="
    r"([^&\s]+)"
)
_GIT_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization:\s*(?:basic|bearer)\s+)\S+")
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github"
    r"_pat_[A-Za-z0-9_]{20,})\b"
)


def _diagnostic_text(value: object) -> str:
    """Return diagnostic stream data as safely decoded text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def redact_git_diagnostic(value: object) -> str:
    """Return Git diagnostic text with credential-bearing values redacted."""
    redacted = _GIT_AUTH_HEADER_RE.sub(r"\1" + _REDACTED_VALUE, _diagnostic_text(value))
    redacted = _GIT_SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}={_REDACTED_VALUE}", redacted
    )
    redacted = _GITHUB_TOKEN_RE.sub(_REDACTED_VALUE, redacted)
    redacted = _GIT_URL_RE.sub(_REDACTED_GIT_URL, redacted)
    return _GIT_SCP_REMOTE_RE.sub(_REDACTED_GIT_URL, redacted)


def bounded_git_diagnostic(
    value: object,
    *,
    limit: int = _DEFAULT_DIAGNOSTIC_LIMIT,
) -> str:
    """Return a credential-redacted, bounded Git diagnostic tail."""
    if limit <= 0:
        raise ValueError("Git diagnostic limit must be positive")
    return redact_git_diagnostic(value)[-limit:]
