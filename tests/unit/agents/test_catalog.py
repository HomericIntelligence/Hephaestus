"""Test the canonical harness catalog and its one effort transport rule."""

from __future__ import annotations

from typing import cast

import pytest

from hephaestus.agents.catalog import (
    AGENT_CAPABILITIES,
    DEFAULT_EFFORT_SENTINEL,
    HARNESS_RECORDS,
    AgentCapability,
    EffortRequiresModelError,
    EffortTransport,
    Harness,
    ModelTransport,
    UnsupportedAgentError,
    apply_effort,
    harness_record,
)


def test_records_exist_for_every_harness() -> None:
    """Each harness has one complete record and no model catalog."""
    assert tuple(HARNESS_RECORDS) == tuple(Harness)
    for harness, record in HARNESS_RECORDS.items():
        assert record.name is harness
        assert record.display_name
        assert record.commit_coauthor_name
        assert record.cli_name
        assert record.auth_probe
        assert record.model_transport.effort_transport in EffortTransport
        assert not hasattr(record, "models")
        assert AGENT_CAPABILITIES[harness.value] is record.capabilities


def test_records_keep_divergent_values_in_named_fields() -> None:
    """Shared CLI names do not overwrite the other identity fields."""
    assert harness_record("opencode").cli_name == "opencode"
    assert harness_record("opencode2").cli_name == "opencode"
    assert harness_record("opencode").display_name == "OpenCode"
    assert harness_record("opencode2").display_name == "OpenCode 2"
    assert harness_record("opencode2").commit_coauthor_name == "OpenCode-AI"
    assert harness_record("pi").auto_detectable is False
    assert harness_record("pi").requires_isolation_adapter is True
    assert AgentCapability.FILE_READ in harness_record("pi").capabilities.core_capabilities


@pytest.mark.parametrize(
    ("harness", "transport"),
    [
        (Harness.CLAUDE, EffortTransport.NONE),
        (Harness.CODEX, EffortTransport.CODEX_CONFIG),
        (Harness.PI, EffortTransport.PI_THINKING),
        (Harness.OPENCODE, EffortTransport.OPENCODE_VARIANT),
        (Harness.OPENCODE2, EffortTransport.OPENCODE2_INLINE),
    ],
)
def test_each_harness_records_its_transport(harness: Harness, transport: EffortTransport) -> None:
    """The transport rule comes from the harness record."""
    assert HARNESS_RECORDS[harness].model_transport.effort_transport is transport


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        (EffortTransport.NONE, ()),
        (EffortTransport.CODEX_CONFIG, ("-c", 'model_reasoning_effort="high"')),
        (EffortTransport.OPENCODE_VARIANT, ("--variant", "high")),
        (EffortTransport.PI_THINKING, ("--thinking", "high")),
    ],
)
def test_apply_effort_covers_four_argument_transports(
    transport: EffortTransport, expected: tuple[str, ...]
) -> None:
    """One rule emits the argument form for each transport."""
    transport_record = ModelTransport(grammar="", effort_transport=transport)
    args = apply_effort(transport_record, "provider/model", "high")
    assert args.extra_args == expected
    assert args.model_override is None


def test_apply_effort_covers_inline_transport() -> None:
    """The inline transport overrides the model reference."""
    transport_record = HARNESS_RECORDS[Harness.OPENCODE2].model_transport
    args = apply_effort(transport_record, "provider/model", "high")
    assert args.extra_args == ()
    assert args.model_override == "provider/model#high"
    assert args.emits_effort is True


def test_default_sentinel_emits_no_effort_for_every_transport() -> None:
    """The sentinel keeps the provider's built-in effort default."""
    for record in HARNESS_RECORDS.values():
        args = apply_effort(record.model_transport, "provider/model", DEFAULT_EFFORT_SENTINEL)
        assert not args.emits_effort


def test_empty_effort_emits_no_effort_for_every_transport() -> None:
    """An omitted effort stays omitted at every transport."""
    for record in HARNESS_RECORDS.values():
        args = apply_effort(record.model_transport, "provider/model", "")
        assert not args.emits_effort


def test_pi_thinking_argument_takes_precedence() -> None:
    """An explicit thinking value replaces the effort from the selection."""
    transport_record = HARNESS_RECORDS[Harness.PI].model_transport
    args = apply_effort(transport_record, "m", "high", thinking="medium")
    assert args.extra_args == ("--thinking", "medium")


def test_inline_effort_requires_an_explicit_model() -> None:
    """An inline transport rejects effort without a model."""
    transport_record = HARNESS_RECORDS[Harness.OPENCODE2].model_transport
    with pytest.raises(EffortRequiresModelError) as error:
        apply_effort(transport_record, "", "high")
    assert error.value.effort == "high"


def test_unknown_transport_warns_and_falls_back(caplog: pytest.LogCaptureFixture) -> None:
    """A future transport does not add an unverified argument."""
    transport_record = ModelTransport(grammar="", effort_transport=cast(EffortTransport, "future"))
    with caplog.at_level("WARNING"):
        args = apply_effort(transport_record, "m", "high")
    assert not args.emits_effort
    assert "future" in caplog.text


def test_unsupported_agent_error_is_machine_readable() -> None:
    """Callers read a code and a name, not translated prose."""
    error = UnsupportedAgentError("ghost")
    assert isinstance(error, ValueError)
    assert error.code == "unsupported_agent"
    assert error.agent == "ghost"
    assert str(error) == "Unsupported agent: ghost"
    with pytest.raises(UnsupportedAgentError) as raised:
        harness_record("ghost")
    assert raised.value.agent == "ghost"
