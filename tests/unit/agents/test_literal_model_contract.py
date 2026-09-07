"""Verify literal model names at provider command boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.agents import runtime
from hephaestus.agents.model_selection import parse_model_selection


@pytest.mark.parametrize(
    "name", ["astra", "sol", "fable", "FutureModel", "gpt-6-astra", "private/Model"]
)
def test_literal_codex_command_model(name: str) -> None:
    """The selected model reaches Codex without tier translation."""
    args = runtime._codex_model_args(f"{name}:max")
    assert args[args.index("--model") + 1] == name
    assert 'model_reasoning_effort="max"' in args


@pytest.mark.parametrize("agent", ["claude", "codex", "pi", "opencode"])
def test_provider_accepts_arbitrary_model(agent: str) -> None:
    """A provider adapter must not require a model catalog entry."""
    assert (
        runtime.normalize_provider_model_reference(agent, "FutureModel:high") == "FutureModel:high"
    )


def test_unset_codex_model_uses_tool_default() -> None:
    """An omitted model does not inject a named default into a new session."""
    assert runtime._codex_model_args("", use_default=True) == []


def test_model_parser_preserves_case_and_former_alias() -> None:
    """Parsing removes whitespace and separates effort without translation."""
    selection = parse_model_selection(" ASTRA : max ")
    assert (selection.model, selection.reasoning_effort) == ("ASTRA", "max")


@pytest.mark.parametrize("effort", ["", ":default"])
def test_explicit_pi_model_needs_only_configured_effort(tmp_path: Path, effort: str) -> None:
    """An explicit model does not require a second model in Pi settings."""
    (tmp_path / "settings.json").write_text('{"defaultThinkingLevel":"high"}', encoding="utf-8")
    assert runtime.resolve_pi_model_reference(f"MyModel{effort}", pi_dir=tmp_path) == "MyModel:high"
