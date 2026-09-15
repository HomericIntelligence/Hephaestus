"""Pure helpers for redacting and bounding durable diagnostic text."""

from __future__ import annotations

import re

_DEFAULT_DIAGNOSTIC_LIMIT = 2000
_REDACTED_GIT_URL = "<redacted-git-url>"
_REDACTED_PRIVATE_KEY = "<redacted>"
_REDACTED_VALUE = "<redacted-value>"
_PIPELINE_SENTINEL_KEYS = frozenset({"api_key", "apikey", "client_secret"})
_GIT_URL_RE = re.compile(r"(?:https?|ssh|git)://\S+", re.IGNORECASE)
_GIT_SCP_REMOTE_RE = re.compile(r"(?<![\w./-])(?:[\w.-]+@[\w.-]+|[\w-]+(?:\.[\w-]+)+):\S+")
_GIT_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(access[_-]?token|auth_token|oauth_token|token|api[_-]?key|apikey|"
    r"client[_-]?secret|password|passwd|secret|credential)="
    r"([^&\s]+)"
)
_GIT_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization:\s*(?:basic|bearer)\s+)\S+")
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github"
    r"_pat_[A-Za-z0-9_]{20,})\b"
)
_PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PRIVATE_KEY_END_RE = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
_TRUNCATED_VALUE_BOUNDARY_RE = re.compile(r"[\s,;}\"'\\]")
_TRUNCATED_VALUE_LEADING_WRAPPER_RE = re.compile(r"[ \t\"'\\]*")


def _diagnostic_text(value: object) -> str:
    """Return diagnostic stream data as safely decoded text."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, memoryview):
        return value.tobytes().decode("utf-8", errors="replace")
    return str(value)


def _redact_git_secret_assignment(match: re.Match[str]) -> str:
    """Redact one Git secret assignment and keep a compatible sentinel."""
    key = match.group(1)
    normalized_key = key.casefold().replace("-", "_")
    if normalized_key in _PIPELINE_SENTINEL_KEYS and match.group(2) == "<redacted>":
        return match.group(0)
    return f"{key}={_REDACTED_VALUE}"


def redact_private_key_blocks(value: object) -> str:
    """Redact complete PEM private-key blocks in linear passes."""
    text = _diagnostic_text(value)
    parts: list[str] = []
    cursor = 0
    while begin := _PRIVATE_KEY_BEGIN_RE.search(text, cursor):
        parts.append(text[cursor : begin.start()])
        end = _PRIVATE_KEY_END_RE.search(text, begin.end())
        if end is None:
            parts.append(text[begin.start() :])
            return "".join(parts)
        parts.append(_REDACTED_PRIVATE_KEY)
        cursor = end.end()
    parts.append(text[cursor:])
    return "".join(parts)


def redact_truncated_diagnostic_prefix(text: str) -> str:
    """Mask a diagnostic fragment that can start inside one secret value."""
    end = _PRIVATE_KEY_END_RE.search(text)
    if end is not None:
        return _REDACTED_PRIVATE_KEY + text[end.end() :]
    leading_wrapper = _TRUNCATED_VALUE_LEADING_WRAPPER_RE.match(text)
    fragment_start = leading_wrapper.end() if leading_wrapper is not None else 0
    boundary = _TRUNCATED_VALUE_BOUNDARY_RE.search(text, fragment_start)
    if boundary is None:
        return _REDACTED_VALUE
    return _REDACTED_VALUE + text[boundary.start() :]


def redact_git_diagnostic(value: object) -> str:
    """Return Git diagnostic text with credential-bearing values redacted."""
    redacted = _GIT_AUTH_HEADER_RE.sub(r"\1" + _REDACTED_VALUE, redact_private_key_blocks(value))
    redacted = _GIT_SECRET_ASSIGNMENT_RE.sub(_redact_git_secret_assignment, redacted)
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
