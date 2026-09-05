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
