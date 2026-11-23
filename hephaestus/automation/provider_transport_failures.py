"""Fail-closed classification for explicit provider transport failures.

OpenCode 2 uses ``provider.transport``. Codex uses
``codex_tool_or_provider_failure`` only when the detail also names an SSE idle
timeout. Other Codex tool, sandbox, configuration, and local failures stay
outside this classification.

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
_CODEX_FAILURE_PREFIX = "codex_tool_or_provider_failure"
_CODEX_SSE_IDLE_TIMEOUT = "idle timeout waiting for sse"


def classify_provider_transport(text: object) -> str | None:
    """Return the transport class for a bounded, case-insensitive error window.

    OpenCode requires ``provider.transport``. Codex requires its structured
    failure prefix and explicit SSE idle-timeout evidence. Other errors return
    ``None``.
    """
    if not isinstance(text, str):
        return None
    lowered = text.lower()
    codex_start = lowered.find(_CODEX_FAILURE_PREFIX)
    if codex_start != -1:
        codex_snippet = lowered[codex_start : codex_start + PROVIDER_TRANSPORT_MAX_DETAIL]
        if _CODEX_SSE_IDLE_TIMEOUT in codex_snippet:
            return PROVIDER_TRANSPORT_TIMEOUT
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
    """Return True when *text* names a supported provider transport failure."""
    return classify_provider_transport(text) is not None


def provider_transport_timeout_message(detail: str) -> str:
    """Return one bounded, redacted ``agent_error: provider_transport_timeout:`` string."""
    if not isinstance(detail, str):
        detail = f"{detail!r}"
    cause = classify_provider_transport(detail) or PROVIDER_TRANSPORT_OTHER
    redacted = redact_diagnostic_text(detail)[:PROVIDER_TRANSPORT_MAX_DETAIL]
    return f"agent_error: provider_transport_timeout: {cause}: {redacted}"[:500]
