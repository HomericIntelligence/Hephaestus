"""Centralized sanitization for durable pipeline diagnostics.

Raw stdout/stderr tails can contain source text, issue-controlled content,
URLs, or credentials, and a size bound is not a secrecy boundary.  Every
durable sink — the JSONL event log, item payload state, and review receipts —
routes its diagnostic text through :func:`redact_diagnostic_text` before
persistence.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from hephaestus.diagnostics import bounded_git_diagnostic

_REDACTION = "<redacted>"

# Whole-value token patterns: the entire match is replaced.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub tokens (ghp_/gho_/ghu_/ghs_/ghr_ + 36 base62 chars)
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{36,}\b"),
    # AWS access keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # OpenAI-style sk- tokens
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    # Stripe-style secret keys
    re.compile(r"\bsk_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    # Private key blocks (PEM)
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        flags=re.DOTALL,
    ),
)


def _redact_prefix_pattern(prefix_group: str) -> Callable[[re.Match[str]], str]:
    """Return the replacement callable for one prefix-preserving pattern."""

    def _replace(match: re.Match[str]) -> str:
        suffix = match.group("suffix") if "suffix" in match.groupdict() else ""
        return f"{match.group(prefix_group)}{_REDACTION}{suffix}"

    return _replace


# Prefix-preserving patterns: the captured prefix stays, the value is masked.
_PREFIX_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    # Authorization headers (Bearer / Basic / token)
    (
        re.compile(
            r"(?P<prefix>(?:authorization\s*[:=]\s*)(?:bearer|basic|token)\s+)"
            r"(?P<value>[^\s,;]+)",
            flags=re.IGNORECASE,
        ),
        "prefix",
        "value",
    ),
    # key=value / key:value credential assignments
    (
        re.compile(
            r"(?P<prefix>\b(?:token|api[_-]?key|apikey|access[_-]?token|"
            r"client[_-]?secret|password|passwd|secret)\b\s*[:=]\s*[\"']?)"
            r"(?P<value>[^\s,;}\"'\\\n]+)",
            flags=re.IGNORECASE,
        ),
        "prefix",
        "value",
    ),
    # user:password@ URL authorities (keep scheme+user and the trailing @)
    (
        re.compile(r"(?P<prefix>://[^/\s:@]+:)(?P<value>[^@\s/]+)(?P<suffix>@)"),
        "prefix",
        "value",
    ),
)


def redact_diagnostic_text(text: str) -> str:
    """Return *text* with secret-like content masked for durable persistence.

    Non-secret text is returned unchanged; values that look like credentials
    are replaced with ``<redacted>``.  The function is intentionally
    conservative: over-redaction is preferable to leaking a token into an
    append-only event log.
    """
    redacted = text
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(_REDACTION, redacted)
    for pattern, prefix_group, _value_group in _PREFIX_PATTERNS:
        redacted = pattern.sub(_redact_prefix_pattern(prefix_group), redacted)
    return redacted


def redact_bounded_diagnostic_tails(
    stdout_tail: str, stderr_tail: str, *, limit: int
) -> dict[str, str]:
    """Return non-empty diagnostic tails redacted and bounded for persistence."""
    return {
        key: redact_diagnostic_text(bounded_git_diagnostic(tail, limit=limit))
        for key, tail in (("stdout_tail", stdout_tail), ("stderr_tail", stderr_tail))
        if tail
    }
