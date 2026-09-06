"""Canonical model identifiers and reasoning selectors for agent runtimes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

K2_HORIZON_09B = "IFM/K2-Horizon-0.9B"
K2_HORIZON_37B = "IFM/K2-Horizon-3.7B"
K2_HORIZON_7B = "IFM/K2-Horizon-7B"
K2_HORIZON_32B = "IFM/K2-Horizon-32B"
K2_HORIZON_MOVA_36B_A4B = "IFM/K2-Horizon-MoVA-36B-A4B"
K2_HORIZON_375B_A23B = "IFM/K2-Horizon-375B-A23B"
GPT_6_ASTRA = "gpt-6-astra"
GPT_56_SOL = "gpt-5.6-sol"
GPT_56_TERRA = "gpt-5.6-terra"
GPT_56_LUNA = "gpt-5.6-luna"
PI_THINKING_LEVELS: frozenset[str] = frozenset({"off", "minimal", "low", "medium", "high", "xhigh"})

IFM_MODELS: frozenset[str] = frozenset(
    {
        "IFM/Amber",
        "IFM/AmberChat",
        "IFM/AmberSafe",
        "IFM/Crystal",
        "IFM/CrystalChat",
        "IFM/CrystalChat-7B-Web2Code",
        "IFM/K2",
        "IFM/K2-Chat",
        K2_HORIZON_09B,
        "IFM/K2-Horizon-0.9B-Uno",
        K2_HORIZON_37B,
        K2_HORIZON_7B,
        "IFM/K2-Horizon-7B-FP8",
        "IFM/K2-Horizon-7B-Uno",
        K2_HORIZON_32B,
        "IFM/K2-Horizon-32B-FP8",
        K2_HORIZON_375B_A23B,
        "IFM/K2-Horizon-375B-A23B-FP8",
        K2_HORIZON_MOVA_36B_A4B,
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

_IFM_ALIASES: dict[str, str] = {
    "astra": GPT_6_ASTRA,
    "k2-horizon-0.9": K2_HORIZON_09B,
    "k2-horizon-3.7": K2_HORIZON_37B,
    "k2-horizon-7": K2_HORIZON_7B,
    "k2-horizon-32": K2_HORIZON_32B,
    "k2-horizon-36": K2_HORIZON_MOVA_36B_A4B,
    "k2-horizon-375": K2_HORIZON_375B_A23B,
}


class AgentModelSelection(str):
    """A canonical model identifier and its optional reasoning effort."""

    __slots__ = ()

    def __new__(cls, model: str, reasoning_effort: str = "") -> AgentModelSelection:
        """Create a string-compatible selection with separate model metadata."""
        reference = f"{model}:{reasoning_effort}" if reasoning_effort else model
        return super().__new__(cls, reference)

    @property
    def model(self) -> str:
        """Return the model part of the selection."""
        model, separator, effort = self.rpartition(":")
        return model if separator and effort else str(self)

    @property
    def reasoning_effort(self) -> str:
        """Return the optional reasoning-effort part of the selection."""
        _model, separator, effort = self.rpartition(":")
        return effort if separator and effort else ""

    @property
    def reference(self) -> str:
        """Return the compact model reference used between agent layers."""
        return str(self)


CODEX_ROLE_MODEL_ALIASES: Mapping[str, AgentModelSelection] = MappingProxyType(
    {
        "sol": AgentModelSelection(GPT_56_SOL, "xhigh"),
        "terra": AgentModelSelection(GPT_56_TERRA, "xhigh"),
        "luna": AgentModelSelection(GPT_56_LUNA, "medium"),
    }
)

_CODEX_ROLE_MODEL_IDS = frozenset(
    selection.model for selection in CODEX_ROLE_MODEL_ALIASES.values()
)
_SHORT_MODEL_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_CODEX_LEGACY_ALIASES = frozenset({"fable", "opus", "sonnet", "haiku"})
_CODEX_ALIAS_PREFIXES = tuple(f"{alias}-" for alias in (*CODEX_ROLE_MODEL_ALIASES, "astra"))


def _normalize_model_id(model: str) -> str:
    """Return the canonical identifier for a model ID or a known alias."""
    return _IFM_ALIASES.get(model.lower(), model)


def parse_model_selection(reference: str) -> AgentModelSelection:
    """Split an optional free-form effort from the final colon segment."""
    if isinstance(reference, AgentModelSelection):
        return reference
    value = reference.strip()
    if not value:
        return AgentModelSelection("")
    base, separator, effort = value.rpartition(":")
    if separator and effort.strip():
        return AgentModelSelection(_normalize_model_id(base.strip()), effort.strip())
    return AgentModelSelection(_normalize_model_id(value))


def normalize_model_reference(reference: str) -> str:
    """Return a canonical model reference for a full ID or a known alias."""
    return parse_model_selection(reference).reference


class UnknownModelAliasError(ValueError):
    """Raised when a Codex reference uses an unknown short alias."""


def resolve_codex_model_selection(reference: str) -> AgentModelSelection:
    """Resolve a Codex role alias and preserve its selected effort."""
    selection = parse_model_selection(reference)
    if not selection.model:
        return selection

    model_key = selection.model.casefold()
    for alias_selection in CODEX_ROLE_MODEL_ALIASES.values():
        if model_key == alias_selection.model.casefold():
            default_effort = alias_selection.reasoning_effort
            return AgentModelSelection(
                alias_selection.model,
                selection.reasoning_effort or default_effort,
            )

    role_selection = CODEX_ROLE_MODEL_ALIASES.get(model_key)
    if role_selection is None:
        return selection
    return AgentModelSelection(
        role_selection.model,
        selection.reasoning_effort or role_selection.reasoning_effort,
    )


def validate_codex_role_model_reference(reference: str) -> None:
    """Reject an unknown short alias in a Codex model reference."""
    selection = parse_model_selection(reference)
    model = selection.model
    if not model:
        return

    model_key = model.casefold()
    unknown_alias = bool(_SHORT_MODEL_ALIAS_RE.fullmatch(model)) or model_key.startswith(
        _CODEX_ALIAS_PREFIXES
    )
    if (
        model_key in CODEX_ROLE_MODEL_ALIASES
        or model_key in _CODEX_ROLE_MODEL_IDS
        or model_key in _CODEX_LEGACY_ALIASES
        or model_key == "astra"
        or not unknown_alias
    ):
        return

    raise UnknownModelAliasError(
        f"Unknown Codex model alias {model!r}; use sol, terra, luna, or a full model ID"
    )


__all__ = [
    "CODEX_ROLE_MODEL_ALIASES",
    "GPT_6_ASTRA",
    "GPT_56_LUNA",
    "GPT_56_SOL",
    "GPT_56_TERRA",
    "IFM_MODELS",
    "K2_HORIZON_09B",
    "K2_HORIZON_7B",
    "K2_HORIZON_32B",
    "K2_HORIZON_37B",
    "K2_HORIZON_375B_A23B",
    "K2_HORIZON_MOVA_36B_A4B",
    "PI_THINKING_LEVELS",
    "AgentModelSelection",
    "UnknownModelAliasError",
    "normalize_model_reference",
    "parse_model_selection",
    "resolve_codex_model_selection",
    "validate_codex_role_model_reference",
]
