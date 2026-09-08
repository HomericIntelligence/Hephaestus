"""Tests for canonical model-reference parsing."""

from __future__ import annotations

import pytest

from hephaestus.agents import model_selection


@pytest.mark.parametrize(
    ("reference", "expected_model", "expected_effort"),
    [
        (" astra : max ", "astra", "max"),
        (" private/provider:model : default ", "private/provider:model", "default"),
    ],
)
def test_parse_model_selection_strips_each_segment(
    reference: str,
    expected_model: str,
    expected_effort: str,
) -> None:
    """A model reference strips edge whitespace from its parsed segments."""
    selection = model_selection.parse_model_selection(reference)

    assert selection.model == expected_model
    assert selection.reasoning_effort == expected_effort


def test_normalize_model_reference_preserves_case() -> None:
    """Model names retain their spelling and case."""
    assert model_selection.normalize_model_reference(" ASTRA : max ") == "ASTRA:max"


def test_model_selection_is_immutable() -> None:
    """Selection metadata cannot change after construction."""
    selection = model_selection.AgentModelSelection("MyModel", "high")
    with pytest.raises(AttributeError):
        object.__setattr__(selection, "model", "other")
    with pytest.raises(AttributeError):
        object.__setattr__(selection, "reasoning_effort", "low")
    with pytest.raises(AttributeError):
        _ = selection.__dict__


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("SOL", "SOL"),
        ("terra:high", "terra:high"),
        ("gpt-5.6-luna", "gpt-5.6-luna"),
        ("luna:default", "luna:default"),
        ("gpt-6-astra:future-effort", "gpt-6-astra:future-effort"),
    ],
)
def test_resolve_codex_model_selection_preserves_effort(
    reference: str,
    expected: str,
) -> None:
    """Codex keeps the model and only uses an explicit effort."""
    assert model_selection.resolve_codex_model_selection(reference).reference == expected


@pytest.mark.parametrize("reference", ["unknown", "unknown:high", "terra-lite:high"])
def test_validate_codex_model_reference_accepts_arbitrary_short_names(reference: str) -> None:
    """The provider owns model name validation."""
    model_selection.validate_codex_role_model_reference(reference)


@pytest.mark.parametrize(
    "reference",
    ["", ":high", "private/provider:model:high", "gpt-5.6:future-effort"],
)
def test_validate_codex_model_reference_accepts_full_model_references(reference: str) -> None:
    """Full and provider-qualified model references remain valid."""
    model_selection.validate_codex_role_model_reference(reference)


@pytest.mark.parametrize(
    "reference",
    ["", ":provider-default", "fable:high", "mythos", "claude-preview-99-99", "private/claude"],
)
def test_validate_claude_model_reference_accepts_configured_or_full_reference(
    reference: str,
) -> None:
    """Claude accepts configured aliases and exact provider model IDs."""
    model_selection.validate_claude_model_reference(reference)


@pytest.mark.parametrize("reference", ["unknown", "terra-lite:high"])
def test_validate_claude_model_reference_accepts_arbitrary_names(reference: str) -> None:
    """The provider owns model name validation."""
    model_selection.validate_claude_model_reference(reference)


@pytest.mark.parametrize("reference", ["model\x00", "model\x1b:high"])
def test_model_reference_rejects_control_characters(reference: str) -> None:
    """Literal model names still require safe command input."""
    for validate in (
        model_selection.parse_model_selection,
        model_selection.validate_codex_role_model_reference,
        model_selection.validate_claude_model_reference,
    ):
        with pytest.raises(ValueError, match="control character"):
            validate(reference)
