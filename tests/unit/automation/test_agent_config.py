"""#1441: agent_config consolidates models+timeouts+naming; shims re-export it."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

import pytest

from hephaestus.agents.model_selection import parse_model_selection
from hephaestus.automation import agent_config

EXPECTED_IFM_MODELS = frozenset(
    {
        "IFM/Amber",
        "IFM/AmberChat",
        "IFM/AmberSafe",
        "IFM/Crystal",
        "IFM/CrystalChat",
        "IFM/CrystalChat-7B-Web2Code",
        "IFM/K2",
        "IFM/K2-Chat",
        "IFM/K2-Horizon-0.9B",
        "IFM/K2-Horizon-0.9B-Uno",
        "IFM/K2-Horizon-3.7B",
        "IFM/K2-Horizon-7B",
        "IFM/K2-Horizon-7B-FP8",
        "IFM/K2-Horizon-7B-Uno",
        "IFM/K2-Horizon-32B",
        "IFM/K2-Horizon-32B-FP8",
        "IFM/K2-Horizon-375B-A23B",
        "IFM/K2-Horizon-375B-A23B-FP8",
        "IFM/K2-Horizon-MoVA-36B-A4B",
        "IFM/K2-Horizon-MoVA-36B-A4B-FP8",
        "IFM/K2-Spike-1",
        "IFM/K2-Spike-2",
        "IFM/K2-Think",
        "IFM/K2-Think-V2",
        "IFM/K2-V2",
        "IFM/K2-V2-Instruct",
        "IFM/MegaMath-Llama-3.2-1B",
        "IFM/MegaMath-Llama-3.2-3B",
        "IFM/guru-7B",
        "IFM/guru-32B",
        "IFM/k2-vision-65b",
    }
)


def test_agent_config_exposes_all_three_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    """The merged module answers model, timeout, and naming queries."""
    monkeypatch.delenv("HEPH_PLANNER_MODEL", raising=False)
    assert agent_config.planner_model() == agent_config.OPUS  # models
    assert agent_config.implementer_claude_timeout() == agent_config.AGENT_IMPL_TIMEOUT  # timeouts
    assert agent_config.session_name("R", 1, agent_config.AGENT_PLANNER)  # naming


def test_ifm_registry_contains_each_non_gguf_model_repository() -> None:
    """The public IFM registry is an exact reviewed model snapshot."""
    assert agent_config.IFM_MODELS == EXPECTED_IFM_MODELS
    assert len(agent_config.IFM_MODELS) == 31
    assert not any(model.endswith("-GGUF") for model in agent_config.IFM_MODELS)
    assert "IFM/eval-360-sources" not in agent_config.IFM_MODELS
    assert "IFM/megamath_models" not in agent_config.IFM_MODELS


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("k2-horizon-0.9", "IFM/K2-Horizon-0.9B"),
        ("K2-HORIZON-3.7", "IFM/K2-Horizon-3.7B"),
        ("k2-horizon-7:high", "IFM/K2-Horizon-7B:high"),
        (" k2-horizon-32:xhigh ", "IFM/K2-Horizon-32B:xhigh"),
        ("k2-horizon-36", "IFM/K2-Horizon-MoVA-36B-A4B"),
        ("k2-horizon-375", "IFM/K2-Horizon-375B-A23B"),
    ],
)
def test_ifm_aliases_resolve_to_canonical_model_references(model: str, expected: str) -> None:
    """IFM aliases retain a supported reasoning selector."""
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
def test_unregistered_model_keeps_operator_warning(
    model: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unregistered model remains usable and warns the operator."""
    with caplog.at_level(logging.WARNING, logger=agent_config.__name__):
        assert agent_config.reviewer_model(model, agent="pi") == model

    assert "Unknown model" in caplog.text
    assert model in caplog.text


def test_registered_ifm_model_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """A registered IFM model does not produce an unknown-model warning."""
    with caplog.at_level(logging.WARNING, logger=agent_config.__name__):
        assert (
            agent_config.reviewer_model("IFM/K2-Horizon-0.9B:high", agent="pi")
            == "IFM/K2-Horizon-0.9B:high"
        )

    assert "Unknown model" not in caplog.text


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("astra:future-effort", "gpt-6-astra:future-effort"),
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


@pytest.mark.parametrize("agent", ["opencode", "pi"])
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


@pytest.mark.parametrize("agent", ["opencode", "pi"])
def test_direct_agent_fallback_model_uses_agent_config_default(agent: str) -> None:
    """A direct agent receives no implicit Claude fallback model."""
    assert agent_config.fallback_model(agent=agent) == ""


@pytest.mark.parametrize("agent", ["opencode", "pi"])
def test_direct_agent_role_model_keeps_an_explicit_ifm_alias(agent: str) -> None:
    """An explicit IFM alias overrides a direct agent's configured default."""
    assert agent_config.reviewer_model("k2-horizon-0.9:high", agent=agent) == (
        "IFM/K2-Horizon-0.9B:high"
    )


def test_canonical_jsonl_path_is_dot_safe() -> None:
    """Dot-prefixed cwd segments are encoded (guards #822)."""
    p = agent_config.session_jsonl_path("u", Path("/a/.worktrees/b"))
    assert "-worktrees-" in str(p)


# Parity over EVERY public symbol of each shim — a missing re-export is only an
# AttributeError at the call site, so assert the full surface here.
@pytest.mark.parametrize("shim", ["claude_models", "claude_timeouts", "session_naming"])
def test_shim_reexports_every_public_symbol_identically(shim: str) -> None:
    """Each shim re-exports the exact same object agent_config defines."""
    mod = importlib.import_module(f"hephaestus.automation.{shim}")
    public = [
        n
        for n in dir(mod)
        if not n.startswith("_") and not isinstance(getattr(mod, n), type(importlib))
    ]
    for sym in public:
        assert getattr(mod, sym) is getattr(agent_config, sym), f"{shim}.{sym} drifted"
