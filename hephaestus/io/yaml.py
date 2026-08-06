"""Shared access to the optional PyYAML serialization capability."""

from __future__ import annotations

import importlib
import types

_PYYAML_REQUIRED_MESSAGE = (
    "PyYAML is required for YAML support. Install with: pip install PyYAML"
)


def import_yaml() -> types.ModuleType:
    """Return PyYAML's ``yaml`` module.

    Raises:
        RuntimeError: If PyYAML cannot be imported.

    """
    try:
        return importlib.import_module("yaml")
    except ImportError as exc:
        raise RuntimeError(_PYYAML_REQUIRED_MESSAGE) from exc
