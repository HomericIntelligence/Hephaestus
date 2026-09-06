"""Tests for canonical model-reference parsing."""

from __future__ import annotations

import pytest

from hephaestus.agents import model_selection


@pytest.mark.parametrize(
    ("reference", "expected_model", "expected_effort"),
    [
        (" astra : max ", "gpt-6-astra", "max"),
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


def test_normalize_model_reference_resolves_astra_case_insensitively() -> None:
    """The shared registry owns the Astra alias for every provider boundary."""
    assert model_selection.GPT_6_ASTRA == "gpt-6-astra"
    assert (
        model_selection.normalize_model_reference("ASTRA:max")
        == f"{model_selection.GPT_6_ASTRA}:max"
    )


def test_codex_role_alias_map_is_immutable_and_complete() -> None:
    """The shared Codex map contains the three approved role aliases."""
    assert dict(model_selection.CODEX_ROLE_MODEL_ALIASES) == {
        "sol": model_selection.AgentModelSelection("gpt-5.6-sol", "xhigh"),
        "terra": model_selection.AgentModelSelection("gpt-5.6-terra", "xhigh"),
        "luna": model_selection.AgentModelSelection("gpt-5.6-luna", "medium"),
    }
    with pytest.raises(TypeError):
        model_selection.CODEX_ROLE_MODEL_ALIASES["other"] = model_selection.AgentModelSelection(
            "gpt-5.6-other", "high"
        )  # type: ignore[index]


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("SOL", "gpt-5.6-sol:xhigh"),
        ("terra:high", "gpt-5.6-terra:high"),
        ("gpt-5.6-luna", "gpt-5.6-luna:medium"),
        ("luna:default", "gpt-5.6-luna:default"),
        ("gpt-6-astra:future-effort", "gpt-6-astra:future-effort"),
    ],
)
def test_resolve_codex_model_selection_preserves_effort(
    reference: str,
    expected: str,
) -> None:
    """Codex aliases use defaults only when no effort is supplied."""
    assert model_selection.resolve_codex_model_selection(reference).reference == expected


@pytest.mark.parametrize("reference", ["unknown", "unknown:high"])
def test_validate_codex_model_reference_rejects_unknown_short_alias(reference: str) -> None:
    """Unknown short aliases fail before a Codex process can start."""
    with pytest.raises(model_selection.UnknownModelAliasError, match="Unknown Codex model alias"):
        model_selection.validate_codex_role_model_reference(reference)


@pytest.mark.parametrize(
    "reference",
    ["", ":high", "private/provider:model:high", "gpt-5.6:future-effort"],
)
def test_validate_codex_model_reference_accepts_full_model_references(reference: str) -> None:
    """Full and provider-qualified model references remain valid."""
    model_selection.validate_codex_role_model_reference(reference)
