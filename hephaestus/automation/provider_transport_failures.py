"""Fail-closed classification for OpenCode 2 ``provider.transport`` failures.

Production evidence (2026-11-12/13 runs, LLM360/comet): gateway congestion
exhausts the Bun header wait, opencode aborts, and emits
``opencode2_fatal_error_event: provider.transport: Timed out waiting for
response headers``. 47 of 107 planner jobs died this way, with job durations
clustering at ~630 s and ~1,221 s (~300 s per header wait plus startup), and
failure windows clustering in time across two independent coordinators in the
same UTC hours — gateway congestion, not per-run behavior and not model
behavior (jobs with response headers within budget completed normally).

This module classifies the class explicitly and propagates it in bounded,
redacted form. It does NOT auto-retry: a blind retry immediately after a
header timeout competes with the same congestion queue (see issue #3637's
retry problem statement). Retry/backoff/summary visibility tracked separately.
"""

from __future__ import annotations

from hephaestus.automation.pipeline.diagnostics import redact_diagnostic_text

PROVIDER_TRANSPORT_PREFIX = "provider.transport"
PROVIDER_TRANSPORT_TIMEOUT = "provider_transport_timeout"
PROVIDER_TRANSPORT_OTHER = "provider_transport_other"
PROVIDER_TRANSPORT_MAX_DETAIL = 160

_HEADER_TIMEOUT_HINTS = (
    "timed out waiting for response headers",
    "response headers",
    "header timeout",
    "waiting for response",
)
_GENERIC_TIMEOUT_HINTS = ("timed out", "timeout")


def classify_provider_transport(text: object) -> str | None:
    """Return the provider.transport class for supported OpenCode 2 error text.

    Recognises ``opencode2_fatal_error_event: provider.transport: ...`` and
    bare ``provider.transport`` payloads; case-insensitive; bounded to a
    window around the marker.  Returns ``None`` when no provider.transport
    failure is named.
    """
    if not isinstance(text, str):
        return None
    lowered = text.lower()
    start = lowered.find(PROVIDER_TRANSPORT_PREFIX)
    if start == -1:
        return None
    snippet = lowered[start : start + PROVIDER_TRANSPORT_MAX_DETAIL]
    if any(hint in snippet for hint in _HEADER_TIMEOUT_HINTS):
        return PROVIDER_TRANSPORT_TIMEOUT
    if any(hint in snippet for hint in _GENERIC_TIMEOUT_HINTS):
        return PROVIDER_TRANSPORT_TIMEOUT
    return PROVIDER_TRANSPORT_OTHER


def is_provider_transport_error(text: object) -> bool:
    """Return True when *text* names a provider.transport failure."""
    return classify_provider_transport(text) is not None


def provider_transport_timeout_message(detail: str) -> str:
    """Return one bounded, redacted ``agent_error: provider_transport_timeout:`` string."""
    if not isinstance(detail, str):
        detail = f"{detail!r}"
    cause = classify_provider_transport(detail) or PROVIDER_TRANSPORT_OTHER
    redacted = redact_diagnostic_text(detail)[:PROVIDER_TRANSPORT_MAX_DETAIL]
    return f"agent_error: provider_transport_timeout: {cause}: {redacted}"[:500]
