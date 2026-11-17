"""Fail-closed classification for provider-auth agent deaths (#3561).

Evidence from the r2/r3c runs: the ``provider.auth`` signature persisted
for entire runs while fresh probes from independent processes kept
succeeding, and the failed children were new subprocesses spawned from
the same frozen environment as every other child of the loop.  There is
no credential re-read or refresh hook in that spawn path to retry
against, so a bounded retry here would only buy additional burned time
with no chance of success.  This module implements the part that does
fail closed: detect the class, redact the detail, and propagate a
named cause so the lane stops silently and an operator sees the true
reason instead of a generic ``agent_error``.

Retry stays deliberately unsupported until a refresh hook exists; a
``provider_auth`` error is not evidence of transience after the
r2/r3c 46-death-persistent signature.
"""

from __future__ import annotations

from hephaestus.automation.pipeline.diagnostics import redact_diagnostic_text

PROVIDER_AUTH_PREFIX = "provider.auth"
PROVIDER_AUTH_ERROR_CLASS = "provider_auth"
PROVIDER_AUTH_MAX_DETAIL = 160

PROVIDER_AUTH_INVALID_OR_MISSING_KEY = "provider_auth_invalid_or_missing_api_key"
PROVIDER_AUTH_TOKEN_EXPIRED = "provider_auth_token_expired"
PROVIDER_AUTH_RATE_OR_QUOTA = "provider_auth_rate_or_quota"
PROVIDER_AUTH_OTHER = "provider_auth_other"

_TOKEN_EXPIRY_HINTS = ("expired", "expiry", "expire")
_RATE_HINTS = ("rate", "quota", "exhaust", "limit")
_INVALID_KEY_HINTS = ("invalid", "missing")


def classify_agent_error(text: object) -> str | None:
    """Return the provider-auth class for supported OpenCode 2 error text.

    Recognises ``opencode2_fatal_error_event: provider.auth: ...`` and
    bare ``provider.auth`` payloads; case-insensitive; bounded to a
    window around the marker.  Returns ``None`` when no provider-auth
    failure is named.
    """
    if not isinstance(text, str):
        return None
    lowered = text.lower()
    start = lowered.find(PROVIDER_AUTH_PREFIX)
    if start == -1:
        return None
    snippet = lowered[start : start + PROVIDER_AUTH_MAX_DETAIL]
    if "invalid" in snippet and "api key" in snippet:
        return PROVIDER_AUTH_INVALID_OR_MISSING_KEY
    if any(hint in snippet for hint in _TOKEN_EXPIRY_HINTS):
        return PROVIDER_AUTH_TOKEN_EXPIRED
    if any(hint in snippet for hint in _RATE_HINTS):
        return PROVIDER_AUTH_RATE_OR_QUOTA
    if any(hint in snippet for hint in _INVALID_KEY_HINTS):
        return PROVIDER_AUTH_INVALID_OR_MISSING_KEY
    return PROVIDER_AUTH_OTHER


def is_provider_auth_error(text: object) -> bool:
    """Return True when *text* names a provider-auth failure."""
    return classify_agent_error(text) is not None


def provider_auth_error_message(detail: str) -> str:
    """Return one bounded, redacted ``agent_error: provider_auth:`` string."""
    if not isinstance(detail, str):
        detail = f"{detail!r}"
    cause = classify_agent_error(detail) or PROVIDER_AUTH_OTHER
    redacted = redact_diagnostic_text(detail)[:PROVIDER_AUTH_MAX_DETAIL]
    return f"agent_error: provider_auth: {cause}: {redacted}"[:500]
