"""Backward-compatibility shim. Canonical impl: hephaestus.automation.state.implementer."""

from hephaestus.automation.state.implementer import (
    ImplementationStateManager as ImplementationStateManager,
)

__all__ = ["ImplementationStateManager"]
