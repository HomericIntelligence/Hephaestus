"""Tests for provider.transport classification (#3637)."""

from __future__ import annotations

import pytest

from hephaestus.automation import provider_transport_failures as ptf
from hephaestus.automation.pipeline.job_failures import durable_error_class
from hephaestus.automation.pipeline.worker_pool import _agent_exception_result

HEADER_TIMEOUT_EVENT = (
    "opencode2_fatal_error_event: provider.transport: "
    "Timed out waiting for response headers after 300.0s"
)


def test_classify_header_timeout() -> None:
    """The production signature maps to the timeout class."""
    assert ptf.classify_provider_transport(HEADER_TIMEOUT_EVENT) == ptf.PROVIDER_TRANSPORT_TIMEOUT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "agent_error: opencode2_fatal_error_event: "
            "provider.transport: Timed out waiting for response headers",
            ptf.PROVIDER_TRANSPORT_TIMEOUT,
        ),
        ("provider.transport: response headers deadline hit", ptf.PROVIDER_TRANSPORT_TIMEOUT),
        ("provider.transport: timed out", ptf.PROVIDER_TRANSPORT_TIMEOUT),
        (
            "PROVIDER.TRANSPORT: TIMED OUT WAITING FOR RESPONSE HEADERS",
            ptf.PROVIDER_TRANSPORT_TIMEOUT,
        ),
        ("provider.transport: dns failure", ptf.PROVIDER_TRANSPORT_OTHER),
        ("agent_error: codex_tool_or_provider_failure: x", None),
        ("agent_error: provider_auth: x", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_classify_table(text: object, expected: str | None) -> None:
    """Closed marker-based classification with safe fallbacks."""
    assert ptf.classify_provider_transport(text) == expected
    assert ptf.is_provider_transport_error(text) is (expected is not None)


def test_message_is_bounded_redacted_and_named() -> None:
    """The propagated agent_error is named, redacted, and bounded."""
    text = HEADER_TIMEOUT_EVENT + " key=sk-ABCDEFGHIJKLMNOPQRSTUVWX end"
    message = ptf.provider_transport_timeout_message(text)
    assert message.startswith("agent_error: provider_transport_timeout: ")
    assert ptf.PROVIDER_TRANSPORT_TIMEOUT in message
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in message
    assert len(message) <= 500


def test_durable_error_class_maps_the_new_prefix() -> None:
    """The durable log classifier maps the new prefix ahead of generic agent_error."""
    message = ptf.provider_transport_timeout_message(HEADER_TIMEOUT_EVENT)
    assert durable_error_class(message) == "provider_transport_timeout"
    assert durable_error_class("agent_error: unrelated") == "agent_error"


def test_agent_exception_result_named_for_transport_timeout() -> None:
    """_agent_exception_result reports the named class for transport deaths."""
    from hephaestus.agents.runtime import AgentExecutionError

    result = _agent_exception_result(AgentExecutionError(HEADER_TIMEOUT_EVENT))
    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("agent_error: provider_transport_timeout: ")
    assert ptf.PROVIDER_TRANSPORT_TIMEOUT in result.error


def test_auth_precedence_and_legacy_unchanged() -> None:
    """Auth class still wins when both markers appear; non-provider errors unchanged."""
    from hephaestus.agents.runtime import AgentExecutionError

    from hephaestus.automation import provider_auth_resilience as par

    mixed = (
        "agent_error: provider.auth: invalid or missing API key; also provider.transport: timed out"
    )
    assert par.classify_agent_error(mixed) is not None
    result = _agent_exception_result(AgentExecutionError(mixed))
    assert result.error is not None and result.error.startswith("agent_error: provider_auth:")

    plain = _agent_exception_result(AgentExecutionError("timeout: read timed out"))
    assert plain.error == "agent_error: timeout: read timed out"[:500]
