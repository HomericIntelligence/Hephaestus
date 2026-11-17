"""Tests for the fail-closed provider-auth agent-error classifier (#3561)."""

from __future__ import annotations

import pytest

from hephaestus.automation import provider_auth_resilience as par
from hephaestus.automation.pipeline.job_failures import durable_error_class
from hephaestus.automation.pipeline.worker_pool import _agent_exception_result

INVALID_KEY_EVENT = (
    "opencode2_fatal_error_event: provider.auth: "
    "invalid or missing API key: 78e2-expired"
)


def test_classify_invalid_or_missing_key() -> None:
    """The canonical r2/r3c signature maps to the named class."""
    assert par.classify_agent_error(INVALID_KEY_EVENT) == (
        par.PROVIDER_AUTH_INVALID_OR_MISSING_KEY
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("agent_error: opencode2_fatal_error_event: provider.auth: token expired",
         par.PROVIDER_AUTH_TOKEN_EXPIRED),
        ("provider.auth: token expiry noticed by gateway",
         par.PROVIDER_AUTH_TOKEN_EXPIRED),
        ("provider.auth: rate limit exceeded",
         par.PROVIDER_AUTH_RATE_OR_QUOTA),
        ("provider.auth: quota exhausted",
         par.PROVIDER_AUTH_RATE_OR_QUOTA),
        ("provider.auth: some other auth condition",
         par.PROVIDER_AUTH_OTHER),
        ("PROVIDER.AUTH: INVALID OR MISSING API KEY",
         par.PROVIDER_AUTH_INVALID_OR_MISSING_KEY),
        ("agent_error: codex_tool_or_provider_failure: exit rc=1", None),
        ("agent_error: timeout: read timed out", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_classify_agent_error_table(text: object, expected: str | None) -> None:
    """Classification is a closed marker-based rule with safe fallbacks."""
    assert par.classify_agent_error(text) == expected
    assert par.is_provider_auth_error(text) is (expected is not None)


def test_message_is_bounded_redacted_and_named() -> None:
    """The propagated agent_error is named, redacted, and bounded."""
    text = (
        "opencode2_fatal_error_event: provider.auth: invalid or missing API key"
        " leaked sk-ABCDEFGHIJKLMNOPQRSTUVWX end"
    )
    message = par.provider_auth_error_message(text)
    assert message.startswith("agent_error: provider_auth: ")
    assert par.PROVIDER_AUTH_INVALID_OR_MISSING_KEY in message
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in message
    assert len(message) <= 500


def test_durable_error_class_recognizes_new_prefix() -> None:
    """The durable log classifier maps the new prefix ahead of generic agent_error."""
    message = par.provider_auth_error_message(INVALID_KEY_EVENT)
    assert durable_error_class(message) == "provider_auth"
    assert durable_error_class("agent_error: unrelated") == "agent_error"


def test_agent_exception_result_named_for_provider_auth() -> None:
    """_agent_exception_result reports the named class for auth deaths."""
    from hephaestus.agents.runtime import AgentExecutionError

    result = _agent_exception_result(AgentExecutionError(INVALID_KEY_EVENT))
    assert result.ok is False
    assert result.error is not None
    assert result.error.startswith("agent_error: provider_auth: ")
    assert par.PROVIDER_AUTH_INVALID_OR_MISSING_KEY in result.error


def test_agent_exception_result_unchanged_for_non_auth() -> None:
    """Non-provider-auth AgentExecutionError keeps the legacy shape."""
    from hephaestus.agents.runtime import AgentExecutionError

    result = _agent_exception_result(AgentExecutionError("timeout: read timed out"))
    assert result.ok is False
    assert result.error == "agent_error: timeout: read timed out"[:500]
