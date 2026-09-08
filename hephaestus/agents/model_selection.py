"""Literal model names and optional provider effort selectors."""

from __future__ import annotations

PI_THINKING_LEVELS: frozenset[str] = frozenset({"off", "minimal", "low", "medium", "high", "xhigh"})


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


def parse_model_selection(reference: str) -> AgentModelSelection:
    """Split an optional free-form effort from the final nonempty colon segment."""
    if not isinstance(reference, str):
        raise TypeError("model reference must be a string")
    if any(ord(char) < 32 and not char.isspace() for char in reference):
        raise ValueError("model reference contains a control character")
    value = reference.strip()
    if not value:
        return AgentModelSelection("")
    base, separator, effort = value.rpartition(":")
    if separator and effort.strip():
        return AgentModelSelection(base.strip(), effort.strip())
    return AgentModelSelection(value)


def normalize_model_reference(reference: str) -> str:
    """Return a whitespace-normalized reference without model translation."""
    return parse_model_selection(reference).reference


def resolve_codex_model_selection(reference: str) -> AgentModelSelection:
    """Return the literal Codex model and optional effort."""
    return parse_model_selection(reference)


def validate_codex_role_model_reference(reference: str) -> None:
    """Validate a model string without a model catalog."""
    parse_model_selection(reference)


def validate_claude_model_reference(reference: str) -> None:
    """Validate a model string without a model catalog."""
    parse_model_selection(reference)


__all__ = [
    "PI_THINKING_LEVELS",
    "AgentModelSelection",
    "normalize_model_reference",
    "parse_model_selection",
    "resolve_codex_model_selection",
    "validate_claude_model_reference",
    "validate_codex_role_model_reference",
]
