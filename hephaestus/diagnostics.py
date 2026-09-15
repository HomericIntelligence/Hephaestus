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
_PRIVATE_KEY_BEGIN_FRAGMENT_RE = re.compile(r"[A-Z -]*-")
_PRIVATE_KEY_PAYLOAD_FRAGMENT_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_PRIVATE_KEY_PAYLOAD_LINE_RE = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")
_TRUNCATED_VALUE_BOUNDARY_RE = re.compile(r"[\s,;}\"'\\]")
_TRUNCATED_VALUE_WRAPPERS = " \t\r\"'\\"
_TRUNCATED_SEPARATOR_PREFIXES = (r"\r\n", r"r\n", "\r\n", r"\n", "\n", "n")
_PRIVATE_KEY_PAYLOAD_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


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
            parts.append(_REDACTED_PRIVATE_KEY)
            return "".join(parts)
        parts.append(_REDACTED_PRIVATE_KEY)
        cursor = end.end()
    parts.append(text[cursor:])
    return "".join(parts)


def _diagnostic_line_end(text: str, start: int) -> tuple[int, int, bool]:
    """Return one actual or escaped diagnostic line boundary."""
    cursor = start
    while cursor < len(text):
        if text.startswith("\r\n", cursor):
            return cursor, 2, False
        if text[cursor] == "\n":
            return cursor, 1, False
        if text.startswith(r"\r\n", cursor):
            return cursor, 4, True
        if text.startswith(r"\n", cursor):
            return cursor, 2, True
        cursor += 1
    return len(text), 0, False


def _leading_separator_prefix_width(text: str) -> int:
    """Return the width of a full or boundary-cut leading separator."""
    for prefix in _TRUNCATED_SEPARATOR_PREFIXES:
        if text.startswith(prefix):
            return len(prefix)
    return 0


def _ambiguous_private_key_prefix(
    *,
    marker_fragment: bool,
    payload_lines: int,
    first_line_wrapped: bool,
    has_full_line: bool,
    continuation_has_full_line: bool,
    terminating_payload_fragment: bool,
    leading_separator_fragment: bool,
) -> bool:
    """Return whether parsed prefix lines can be a PEM continuation."""
    if marker_fragment:
        return payload_lines > 0 or terminating_payload_fragment
    payload_fragments = payload_lines + int(terminating_payload_fragment)
    if payload_fragments < 2:
        return False
    if leading_separator_fragment:
        return has_full_line
    return continuation_has_full_line if first_line_wrapped else has_full_line


def _payload_terminator(text: str, start: int, end: int) -> tuple[int, bool]:
    """Return a PEM fragment boundary and whether the fragment has payload."""
    fragment = text[start:end].lstrip(_TRUNCATED_VALUE_WRAPPERS)
    payload_start = end - len(fragment)
    payload_end = 0
    while payload_end < len(fragment) and fragment[payload_end] in _PRIVATE_KEY_PAYLOAD_CHARACTERS:
        payload_end += 1
    return payload_start + payload_end, payload_end > 0


def _private_key_continuation_boundary(text: str) -> int | None:
    """Return the first safe index after an ambiguous PEM continuation."""
    cursor = _leading_separator_prefix_width(text)
    leading_separator_fragment = cursor > 0
    payload_lines = 0
    has_full_line = False
    first_line_wrapped = False
    continuation_has_full_line = False
    marker_fragment = False
    line_index = 0
    while cursor <= len(text):
        line_end, separator_width, _separator_is_literal = _diagnostic_line_end(text, cursor)
        raw_line = text[cursor:line_end]
        line = raw_line.strip(_TRUNCATED_VALUE_WRAPPERS)
        if line_index == 0:
            first_line_wrapped = len(raw_line.lstrip(_TRUNCATED_VALUE_WRAPPERS)) != len(raw_line)
        if line_index == 0 and _PRIVATE_KEY_BEGIN_FRAGMENT_RE.fullmatch(line):
            marker_fragment = True
        elif line and _PRIVATE_KEY_PAYLOAD_FRAGMENT_RE.fullmatch(line):
            payload_lines += 1
            if _PRIVATE_KEY_PAYLOAD_LINE_RE.fullmatch(line):
                has_full_line = True
                if line_index > 0:
                    continuation_has_full_line = True
        else:
            payload_boundary, has_terminating_payload = _payload_terminator(text, cursor, line_end)
            ambiguous = _ambiguous_private_key_prefix(
                marker_fragment=marker_fragment,
                payload_lines=payload_lines,
                first_line_wrapped=first_line_wrapped,
                has_full_line=has_full_line,
                continuation_has_full_line=continuation_has_full_line,
                terminating_payload_fragment=has_terminating_payload,
                leading_separator_fragment=leading_separator_fragment,
            )
            if not ambiguous:
                return None
            return payload_boundary
        if separator_width == 0:
            ambiguous = _ambiguous_private_key_prefix(
                marker_fragment=marker_fragment,
                payload_lines=payload_lines,
                first_line_wrapped=first_line_wrapped,
                has_full_line=has_full_line,
                continuation_has_full_line=continuation_has_full_line,
                terminating_payload_fragment=False,
                leading_separator_fragment=leading_separator_fragment,
            )
            return len(text) if ambiguous else None
        cursor = line_end + separator_width
        line_index += 1
    return None


def redact_truncated_diagnostic_prefix(text: str) -> str:
    """Mask a diagnostic fragment that can start inside one secret value."""
    end = _PRIVATE_KEY_END_RE.search(text)
    if end is not None:
        return _REDACTED_PRIVATE_KEY + text[end.end() :]
    private_key_boundary = _private_key_continuation_boundary(text)
    if private_key_boundary is not None:
        return _REDACTED_VALUE + text[private_key_boundary:]
    fragment_start = _leading_separator_prefix_width(text)
    remainder = text[fragment_start:]
    fragment_start += len(remainder) - len(remainder.lstrip(_TRUNCATED_VALUE_WRAPPERS))
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
