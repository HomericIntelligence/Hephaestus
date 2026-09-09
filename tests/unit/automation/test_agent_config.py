"""Test canonical model, timeout, and session configuration."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest

from hephaestus.agents.model_selection import parse_model_selection
from hephaestus.automation import agent_config


def test_agent_config_exposes_all_three_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merged module answers model, timeout, and naming queries."""
    monkeypatch.delenv("HEPH_PLANNER_MODEL", raising=False)
    assert agent_config.planner_model() == ""  # models
    assert agent_config.implementer_claude_timeout() == agent_config.AGENT_IMPL_TIMEOUT  # timeouts
    assert agent_config.session_name("R", 1, agent_config.AGENT_PLANNER)  # naming


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("k2-horizon-0.9", "k2-horizon-0.9"),
        ("K2-HORIZON-3.7", "K2-HORIZON-3.7"),
        ("k2-horizon-7:high", "k2-horizon-7:high"),
        (" k2-horizon-32:xhigh ", "k2-horizon-32:xhigh"),
        ("k2-horizon-36", "k2-horizon-36"),
        ("k2-horizon-375", "k2-horizon-375"),
    ],
)
def test_former_ifm_aliases_remain_literal(model: str, expected: str) -> None:
    """Former aliases retain their literal names and effort."""
    assert agent_config.normalize_model_reference(model) == expected


def test_unregistered_model_selection_stays_string_compatible() -> None:
    """A compact unregistered model reference stays string-compatible."""
    model = "ollama/qwen:high"

    assert agent_config.normalize_model_reference(model) == model


@pytest.mark.parametrize(
    ("reference", "expected_model", "expected_effort"),
    [
        ("gpt-6-astra:max", "gpt-6-astra", "max"),
        ("gpt-6-astra:future-effort", "gpt-6-astra", "future-effort"),
        ("private/provider:model:ultra", "private/provider:model", "ultra"),
        (":provider-default", "", "provider-default"),
        ("private/provider:model", "private/provider", "model"),
        ("private/provider:model:", "private/provider:model:", ""),
        ("model:   ", "model:", ""),
    ],
)
def test_model_selection_uses_the_final_colon_for_any_nonempty_effort(
    reference: str,
    expected_model: str,
    expected_effort: str,
) -> None:
    """The final nonempty segment is an open-ended provider effort value."""
    selection = parse_model_selection(reference)

    assert selection.model == expected_model
    assert selection.reasoning_effort == expected_effort


@pytest.mark.parametrize("model", ["private-model", "ollama/qwen:high"])
def test_arbitrary_model_does_not_warn(
    model: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An arbitrary model name does not cause a warning."""
    with caplog.at_level(logging.WARNING, logger=agent_config.__name__):
        assert agent_config.reviewer_model(model, agent="pi") == model

    assert not caplog.records


def test_registered_ifm_model_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """A registered IFM model does not produce an unknown-model warning."""
    with caplog.at_level(logging.WARNING, logger=agent_config.__name__):
        assert (
            agent_config.reviewer_model("IFM/K2-Horizon-0.9B:high", agent="pi")
            == "IFM/K2-Horizon-0.9B:high"
        )

    assert "Unknown model" not in caplog.text


def test_claude_provider_default_selection_stays_explicit_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An explicit empty base remains different from an omitted model."""
    with caplog.at_level(logging.WARNING, logger=agent_config.__name__):
        resolved = agent_config.reviewer_model(":provider-default", agent="claude")

    assert resolved == ":provider-default"
    assert "Unknown model" not in caplog.text


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("astra:future-effort", "astra:future-effort"),
        ("gpt-6-astra:future-effort", "gpt-6-astra:future-effort"),
    ],
)
def test_registered_astra_model_does_not_warn(
    model: str,
    expected: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The supported Astra model does not produce an unknown-model warning."""
    with caplog.at_level(logging.WARNING, logger=agent_config.__name__):
        assert agent_config.reviewer_model(model, agent="codex") == expected

    assert "Unknown model" not in caplog.text


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("sol", "sol"),
        ("terra:high", "terra:high"),
        ("luna", "luna"),
    ],
)
def test_codex_former_aliases_remain_literal(
    reference: str,
    expected: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex keeps former aliases without an implicit effort."""
    with caplog.at_level(logging.WARNING, logger=agent_config.__name__):
        assert agent_config.reviewer_model(reference, agent="codex") == expected

    assert "Unknown model" not in caplog.text


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode", "pi"])
@pytest.mark.parametrize(
    "resolver",
    [
        agent_config.planner_model,
        agent_config.implementer_model,
        agent_config.reviewer_model,
        agent_config.advise_model,
        agent_config.learn_model,
    ],
)
def test_direct_agent_role_model_uses_agent_config_default(agent: str, resolver: object) -> None:
    """A direct agent receives no implicit Claude role model."""
    assert callable(resolver)
    assert resolver(agent=agent) == ""


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode", "pi"])
def test_direct_agent_fallback_model_uses_agent_config_default(agent: str) -> None:
    """A direct agent receives no implicit Claude fallback model."""
    assert agent_config.fallback_model(agent=agent) == ""


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode", "pi"])
def test_direct_agent_role_model_keeps_an_explicit_ifm_alias(agent: str) -> None:
    """An explicit IFM alias overrides a direct agent's configured default."""
    assert agent_config.reviewer_model("k2-horizon-0.9:high", agent=agent) == (
        "k2-horizon-0.9:high"
    )


def test_canonical_jsonl_path_is_dot_safe() -> None:
    """Dot-prefixed cwd segments are encoded (guards #822)."""
    p = agent_config.session_jsonl_path("u", Path("/a/.worktrees/b"))
    assert "-worktrees-" in str(p)


@pytest.mark.parametrize("module", ["claude_models", "claude_timeouts", "session_naming"])
def test_retired_configuration_modules_cannot_be_imported(module: str) -> None:
    """Callers must use the canonical agent configuration module."""
    qualified_name = f"hephaestus.automation.{module}"
    with pytest.raises(ModuleNotFoundError) as error:
        importlib.import_module(qualified_name)
    assert error.value.name == qualified_name
