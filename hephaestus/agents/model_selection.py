"""Canonical model identifiers and reasoning selectors for agent runtimes."""

from __future__ import annotations

K2_HORIZON_09B = "IFM/K2-Horizon-0.9B"
K2_HORIZON_37B = "IFM/K2-Horizon-3.7B"
K2_HORIZON_7B = "IFM/K2-Horizon-7B"
K2_HORIZON_32B = "IFM/K2-Horizon-32B"
K2_HORIZON_MOVA_36B_A4B = "IFM/K2-Horizon-MoVA-36B-A4B"
K2_HORIZON_375B_A23B = "IFM/K2-Horizon-375B-A23B"
GPT_6_ASTRA = "gpt-6-astra"
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

    model: str
    reasoning_effort: str

    def __new__(cls, model: str, reasoning_effort: str = "") -> AgentModelSelection:
        """Create a string-compatible selection with separate model metadata."""
        reference = f"{model}:{reasoning_effort}" if reasoning_effort else model
        selection = super().__new__(cls, reference)
        selection.model = model
        selection.reasoning_effort = reasoning_effort
        return selection

    @property
    def reference(self) -> str:
        """Return the compact model reference used between agent layers."""
        return str(self)


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


__all__ = [
    "GPT_6_ASTRA",
    "IFM_MODELS",
    "K2_HORIZON_09B",
    "K2_HORIZON_7B",
    "K2_HORIZON_32B",
    "K2_HORIZON_37B",
    "K2_HORIZON_375B_A23B",
    "K2_HORIZON_MOVA_36B_A4B",
    "PI_THINKING_LEVELS",
    "AgentModelSelection",
    "normalize_model_reference",
    "parse_model_selection",
]
